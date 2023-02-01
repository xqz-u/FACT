import itertools as it
import logging
import multiprocessing as mp
import pprint
from pathlib import Path
from typing import Dict, Sequence, Tuple

from scipy import sparse

import config
import constants
import datasets
import recommender_models as recsys
import utils

logger = logging.getLogger(__name__)


def save_hyperparams_and_metrics(filename: Path, hparams: dict, info: dict):
    header = f"{','.join(list(hparams) + list(info))}"
    content = list(hparams.values()) + list(info.values())
    content = ",".join(list(map(str, content)))
    with open(filename, "w") as fd:
        fd.write(header + "\n")
        fd.write(content + "\n")
    logger.debug("Saved best model hyperparams and info to %s", filename)


def train_eval_one_configuration(
    hp: dict, rest
) -> Tuple[Dict[str, float], Dict[str, float], recsys.RecommenderType]:
    model_class, hparams_names, train, valid, k = rest
    hp = dict(zip(hparams_names, hp))
    model = model_class(**hp)
    logger_ = getattr(model, "logger", logger)
    logger_.info("Grid search hparams: %s", hp)
    model.train(train)
    return model.validate(train, valid, k=k), hp, model


def search_best_model(
    train_mat: sparse.csr_matrix,
    valid_mat: sparse.csr_matrix,
    hparams_names: Sequence[str],
    hparams_flat: Sequence[float],
    seedgen: utils.SeedSequence,
    model_class: recsys.RecommenderType,
    conf: config.Configuration,
) -> dict:
    repeat_args = [
        [model_class, list(hparams_names), train_mat, valid_mat, conf.evaluation_k]
    ] * len(hparams_flat)
    args = list(zip(hparams_flat, repeat_args))

    if conf.parallel:
        n_procs = min(len(hparams_flat), mp.cpu_count())
        logger.info("Spawning %d processes for grid search", n_procs)
        with mp.Pool(processes=n_procs) as pool:
            res = pool.starmap(train_eval_one_configuration, args)
    else:
        res = [train_eval_one_configuration(*arg) for arg in args]

    best_metrics, best_hparams, best_model = max(
        res, key=lambda el: el[0][conf.recommender_evaluation_metric]
    )
    return {
        "model": best_model,
        "hparams": best_hparams,
        "info": {
            **best_metrics,
            "model": best_model.__class__,
            "used": conf.recommender_evaluation_metric,
            "seed_start": seedgen.start,
        },
    }


def search_ground_truth(
    dataset: str,
    train_mat: sparse.csr_matrix,
    valid_mat: sparse.csr_matrix,
    seedgen: utils.SeedSequence,
    conf: config.Configuration,
) -> recsys.RecommenderType:
    ground_truth_model_class = conf.ground_truth_models[dataset]
    logger.info("Ground truth for %s, model: %s", dataset, ground_truth_model_class)

    hyperparams = constants.ground_truth_hparams[ground_truth_model_class]
    logger.info("Hyperparameters in grid search for dataset %s:", dataset)
    logger.info(pprint.pformat(hyperparams))

    best_dict = search_best_model(
        train_mat,
        valid_mat,
        hyperparams.keys(),
        list(it.product(*hyperparams.values())),
        seedgen,
        ground_truth_model_class,
        conf,
    )

    model_save_path = conf.ground_truth_files[dataset]
    best_dict["model"].save(model_save_path)
    logger.debug("Saved best model to %s", model_save_path)

    hparams_save_path = f"{model_save_path.parent / model_save_path.stem}_hparams.txt"
    save_hyperparams_and_metrics(
        hparams_save_path, best_dict["hparams"], best_dict["info"]
    )

    return best_dict["model"]


def search_recommender(
    dataset: str,
    train_mat: sparse.csr_matrix,
    valid_mat: sparse.csr_matrix,
    seedgen: utils.SeedSequence,
    conf: config.Configuration,
) -> recsys.RecommenderType:
    model_class = conf.recommender_models[dataset]
    hyperparams = constants.recommender_hparams[model_class]
    logger.info("%s, Hyperparameters in recommender grid search:", model_class)
    logger.info(pprint.pformat(hyperparams))

    hyperparams_inner = hyperparams.copy()
    factors = hyperparams_inner.pop("factors")
    hyperparams_inner_flat = list(it.product(*hyperparams_inner.values()))

    # save the best model for each factor
    for factor in factors:
        # NOTE (factor, ) + hparams relies on the keys of hyperparams to be,
        # in order, factors,regularization,alpha; there is an easy fix for
        # this not implemented rn
        hparams = list((factor,) + hp for hp in hyperparams_inner_flat)
        best_dict = search_best_model(
            train_mat,
            valid_mat,
            hyperparams.keys(),
            hparams,
            seedgen,
            model_class,
            conf,
        )

        model_base_save_path = conf.recommender_dirs[dataset] / conf.model_base_name
        best_dict["model"].save(f"{model_base_save_path}_factors_{factor}.npz")
        logger.debug("Saved best model to %s.npz", model_base_save_path)

        hparams_save_path = f"{model_base_save_path}_factors_{factor}_hparams.txt"
        save_hyperparams_and_metrics(
            hparams_save_path, best_dict["hparams"], best_dict["info"]
        )


def generate_ground_truth(
    dataset_name: str,
    seedgen: utils.SeedSequence,
    conf: config.Configuration,
) -> recsys.RecommenderType:
    logger.info("Loading and preprocessing dataset %s...", dataset_name)
    dataset = datasets.get_dataset(dataset_name, conf)
    # dataset = utils.normalize(dataset)
    train, valid, _ = utils.train_test_split(
        dataset, conf.train_size, seedgen, valid_prop=conf.validation_size
    )
    return search_ground_truth(dataset_name, train, valid, seedgen, conf)


def generate_recommenders(
    ground_truth_model_path: Path,
    dataset: str,
    seedgen: utils.SeedSequence,
    conf: config.Configuration,
):
    if ground_truth_model_path.exists():
        logger.info("Loading ground truth preferences from %s", ground_truth_model_path)
        ground_truth_model = conf.ground_truth_models[dataset].load(
            ground_truth_model_path
        )
    elif dataset in datasets.DATASETS_RETRIEVE_MAP:
        logger.info("Generating ground truth preferences for dataset: %s", dataset)
        ground_truth_model = generate_ground_truth(dataset, seedgen, conf)
    else:
        logger.error("No recommender system implemented for dataset %s", dataset)
        raise NotImplementedError
    # greater than 2 -> not just ground truth and ground truth hparams
    if len(list(conf.recommender_dirs[dataset].iterdir())) > 2:
        logger.info(
            "%s already contains some files, will not grid search recommender %s",
            conf.recommender_dirs[dataset],
            conf.recommender_models[dataset],
        )
    else:
        preferences = ground_truth_model.preferences
        # preferences = utils.normalize(ground_truth_model.preferences)
        train, valid, _ = utils.train_test_split(
            preferences, conf.train_size, seedgen, valid_prop=conf.validation_size
        )
        logger.info("Start fitting the recommender models.")
        search_recommender(dataset, train, valid, seedgen, conf)
