"""
    Mapping helpers
"""

import numpy as np
import pandas as pd
import scanpy as sc
import torch
import logging

from scipy.sparse.csc import csc_matrix
from scipy.sparse.csr import csr_matrix

from . import mapping_optimizer as mo
from . import utils as ut

# from torch.nn.functional import cosine_similarity

logging.getLogger().setLevel(logging.INFO)


def pp_adatas(adata_sc, adata_sp, genes=None):
    """
    Pre-process AnnDatas so that they can be mapped. Specifically:
    - Remove genes that all entries are zero
    - Find the intersection between adata_sc, adata_sp and given marker gene list, save the intersected markers in two adatas
    - Calculate rna_count_based density priors and save it with adata_sp
    :param adata_sc: 
    :param adata_sp:
    :param genes: List of genes to use. If `None`, all genes are used.
    :return:
    """

    # put all var index to lower case to align
    adata_sc.var.index = [g.lower() for g in adata_sc.var.index]
    adata_sp.var.index = [g.lower() for g in adata_sp.var.index]

    adata_sc.var_names_make_unique()
    adata_sp.var_names_make_unique()

    # remove all-zero-valued genes
    sc.pp.filter_genes(adata_sc, min_cells=1)
    sc.pp.filter_genes(adata_sp, min_cells=1)

    if genes is None:
        # Use all genes
        genes = [g.lower() for g in adata_sc.var.index]
    else:
        genes = list(g.lower() for g in genes)

    # Refine `marker_genes` so that they are shared by both adatas
    genes = list(set(genes) & set(adata_sc.var.index) & set(adata_sp.var.index))
    logging.info(f"{len(genes)} shared marker genes.")

    adata_sc.uns["training_genes"] = genes
    adata_sp.uns["training_genes"] = genes
    logging.info(
        f"training genes list is saved in `uns``training_genes` of both single cell and spatial Anndatas."
    )

    # Calculate density prior as % of rna molecule count
    rna_count_per_spot = adata_sp.X.sum(axis=1)
    adata_sp.obs["rna_count_based_density"] = rna_count_per_spot / np.sum(
        rna_count_per_spot
    )
    logging.info(
        f"rna count based density prior is calculated and saved in `obs``rna_count_based_density` of the spatial Anndata."
    )

    # return adata_sc, adata_sp


def adata_to_cluster_expression(adata, cluster_label, scale=True, add_density=True):
    """
    Convert an AnnData to a new AnnData with cluster expressions. Clusters are based on `label` in `adata.obs`.  The returned AnnData has an observation for each cluster, with the cluster-level expression equals to the average expression for that cluster.
    All annotations in `adata.obs` except `label` are discarded in the returned AnnData.
    If `add_density`, the normalized number of cells in each cluster is added to the returned AnnData as obs.cluster_density.
    :param adata:
    :param cluster_label: cluster_label for aggregating
    """
    try:
        value_counts = adata.obs[cluster_label].value_counts(normalize=True)
    except KeyError as e:
        raise ValueError("Provided label must belong to adata.obs.")
    unique_labels = value_counts.index
    new_obs = pd.DataFrame({cluster_label: unique_labels})
    adata_ret = sc.AnnData(obs=new_obs, var=adata.var, uns=adata.uns)

    X_new = np.empty((len(unique_labels), adata.shape[1]))
    for index, l in enumerate(unique_labels):
        if not scale:
            X_new[index] = adata[adata.obs[cluster_label] == l].X.mean(axis=0)
        else:
            X_new[index] = adata[adata.obs[cluster_label] == l].X.sum(axis=0)
    adata_ret.X = X_new

    if add_density:
        adata_ret.obs["cluster_density"] = adata_ret.obs[cluster_label].map(
            lambda i: value_counts[i]
        )

    return adata_ret


def map_cells_to_space(
    adata_cells,
    adata_space,
    mode="cells",
    adata_map=None,
    device="cuda:0",
    learning_rate=0.1,
    num_epochs=1000,
    d=None,
    cluster_label=None,
    scale=True,
    lambda_d=0,
    lambda_g1=1,
    lambda_g2=0,
    lambda_r=0,
    random_state=None,
    verbose=True,
    density_prior=None,
    experiment=None,
):
    """
        Map single cell data (`adata_sc`) on spatial data (`adata_sp`). If `adata_map`
        is provided, resume from previous mapping.
        Returns a cell-by-spot AnnData containing the probability of mapping cell i on spot j.
        The `uns` field of the returned AnnData contains the training genes.
        :param mode: Tangram mode. Currently supported: `cell`, `clusters`
        :param lambda_d (float): Optional. Hiperparameter for the density term of the optimizer. Default is 0.
        :param lambda_g1 (float): Optional. Hyperparameter for the gene-voxel similarity term of the optimizer. Default is 1.
        :param lambda_g2 (float): Optional. Hyperparameter for the voxel-gene similarity term of the optimizer. Default is 0.
        :param lambda_r (float): Optional. Entropy regularizer for the learned mapping matrix. An higher entropy promotes probabilities of each cell peaked over a narrow portion of space. lambda_r = 0 corresponds to no entropy regularizer. Default is 0.
        :param density_prior (ndarray or string): Spatial density of cells, when is a string, value can be 'rna_count_based' or 'uniform', when is a ndarray, shape = (number_spots,). If not provided, the density term is ignored. This array should satisfy the constraints d.sum() == 1.
        :param experiment: experiment object in comet-ml for logging training in comet-ml
    """

    # check invalid values for arguments
    if lambda_g1 == 0:
        raise ValueError("lambda_g1 cannot be 0.")

    if density_prior is not None and lambda_d == 0:
        raise ValueError("When density_prior is not None, lambda_d cannot be 0.")

    if mode not in ["clusters", "cells"]:
        raise ValueError('Argument "mode" must be "cells" or "clusters"')

    if mode == "clusters" and cluster_label is None:
        raise ValueError("A cluster_label must be specified if mode = clusters.")

    if mode == "clusters":
        adata_cells = adata_to_cluster_expression(
            adata_cells, cluster_label, scale, add_density=True
        )

    # Check if training_genes key exist/is valid in adatas.uns
    if "training_genes" not in adata_cells.uns.keys():
        raise ValueError("Missing tangram parameters. Run `pp_adatas()`.")

    if "training_genes" not in adata_space.uns.keys():
        raise ValueError("Missing tangram parameters. Run `pp_adatas()`.")

    assert list(adata_space.uns["training_genes"]) == list(
        adata_cells.uns["training_genes"]
    )

    # get traiing_genes
    training_genes = adata_cells.uns["training_genes"]

    logging.info("Allocate tensors for mapping.")
    # Allocate tensors (AnnData matrix can be sparse or not)

    if isinstance(adata_cells.X, csc_matrix) or isinstance(adata_cells.X, csr_matrix):
        S = np.array(adata_cells[:, training_genes].X.toarray(), dtype="float32",)
    elif isinstance(adata_cells.X, np.ndarray):
        S = np.array(adata_cells[:, training_genes].X.toarray(), dtype="float32",)
    else:
        X_type = type(adata_cells.X)
        logging.error("AnnData X has unrecognized type: {}".format(X_type))
        raise NotImplementedError

    if isinstance(adata_space.X, csc_matrix) or isinstance(adata_space.X, csr_matrix):
        G = np.array(adata_space[:, training_genes].toarray(), dtype="float32")
    elif isinstance(adata_space.X, np.ndarray):
        G = np.array(adata_space[:, training_genes].X, dtype="float32")
    else:
        X_type = type(adata_space.X)
        logging.error("AnnData X has unrecognized type: {}".format(X_type))
        raise NotImplementedError

    if not S.any(axis=0).all() or not G.any(axis=0).all():
        raise ValueError("Genes with all zero values detected. Run `pp_adatas()`.")

    # define density_prior if 'rna_count_based' is passed to the density_prior argument:
    if density_prior == "rna_count_based":
        density_prior = adata_space.obs["rna_count_based_density"]

    # define density_prior if 'uniform' is passed to the density_prior argument:
    elif density_prior == "uniform":
        density_prior = np.ones(G.shape[0]) / G.shape[0]

    if mode == "cells":
        d = density_prior

    if mode == "clusters":
        d = density_prior
        if d is None:
            d = np.ones(G.shape[0]) / G.shape[0]

    # Choose device
    device = torch.device(device)  # for gpu

    hyperparameters = {
        "lambda_d": lambda_d,  # KL (ie density) term
        "lambda_g1": lambda_g1,  # gene-voxel cos sim
        "lambda_g2": lambda_g2,  # voxel-gene cos sim
        "lambda_r": lambda_r,  # regularizer: penalize entropy
    }

    # # Init hyperparameters
    # if mode == 'cells':
    #     hyperparameters = {
    #         'lambda_d': 0,  # KL (ie density) term
    #         'lambda_g1': 1,  # gene-voxel cos sim
    #         'lambda_g2': 0,  # voxel-gene cos sim
    #         'lambda_r': 0,  # regularizer: penalize entropy
    #     }
    # elif mode == 'clusters':
    #     hyperparameters = {
    #         'lambda_d': 1,  # KL (ie density) term
    #         'lambda_g1': 1,  # gene-voxel cos sim
    #         'lambda_g2': 0,  # voxel-gene cos sim
    #         'lambda_r': 0,  # regularizer: penalize entropy
    #         'd_source': np.array(adata_cells.obs['cluster_density']) # match sourge/target densities
    #     }
    # else:
    #     raise NotImplementedError

    # Train Tangram
    logging.info(
        "Begin training with {} genes in {} mode...".format(len(training_genes), mode)
    )
    mapper = mo.Mapper(
        S=S,
        G=G,
        d=d,
        device=device,
        adata_map=adata_map,
        random_state=random_state,
        **hyperparameters,
    )
    # TODO `train` should return the loss function
    if verbose:
        print_each = 100
    else:
        print_each = None

    mapping_matrix, training_history = mapper.train(
        learning_rate=learning_rate,
        num_epochs=num_epochs,
        print_each=print_each,
        experiment=experiment,
    )

    logging.info("Saving results..")
    adata_map = sc.AnnData(
        X=mapping_matrix,
        obs=adata_cells[:, training_genes].obs.copy(),
        var=adata_space[:, training_genes].obs.copy(),
    )

    # Annotate cosine similarity of each training gene
    G_predicted = adata_map.X.T @ S
    cos_sims = []
    for v1, v2 in zip(G.T, G_predicted.T):
        norm_sq = np.linalg.norm(v1) * np.linalg.norm(v2)
        cos_sims.append((v1 @ v2) / norm_sq)

    df_cs = pd.DataFrame(cos_sims, training_genes, columns=["train_score"])
    df_cs = df_cs.sort_values(by="train_score", ascending=False)
    adata_map.uns["train_genes_df"] = df_cs

    # Annotate sparsity of each training genes
    ut.annotate_gene_sparsity(adata_cells)
    ut.annotate_gene_sparsity(adata_space)
    adata_map.uns["train_genes_df"]["sparsity_sc"] = adata_cells[
        :, training_genes
    ].var.sparsity
    adata_map.uns["train_genes_df"]["sparsity_sp"] = adata_space[
        :, training_genes
    ].var.sparsity
    adata_map.uns["train_genes_df"]["sparsity_diff"] = (
        adata_space[:, training_genes].var.sparsity
        - adata_cells[:, training_genes].var.sparsity
    )

    adata_map.uns["training_history"] = training_history

    return adata_map

