import logging
from collections import abc
from typing import Any, List, cast

import numpy as np
import torch
import torch.nn.functional as F
from einops import rearrange
from torch.utils.data import ConcatDataset, Dataset
from torch_geometric.data import Data

from ocpmodels.common.registry import registry

from .config import (
    DatasetConfig,
    FullyBalancedSamplingConfig,
    OneHotTargetsConfig,
    SamplingConfig,
    SplitDatasetConfig,
    TaskDatasetConfig,
    TemperatureSamplingConfig,
)
from .dataset_transform import dataset_transform, expand_dataset


def _update_graph_value(data: Data, key: str, onehot: torch.Tensor):
    value = getattr(data, key, None)
    assert (value) is not None, f"{key} must be defined."
    if not torch.is_tensor(value):
        value = torch.tensor(value, dtype=torch.float)

    value = cast(torch.Tensor, value)
    value = rearrange(value.view(-1), "1 -> 1 1") * onehot
    setattr(data, f"{key}_onehot", value)


def _update_node_value(data: Data, key: str, onehot: torch.Tensor):
    value = getattr(data, key, None)
    assert (value) is not None, f"{key} must be defined."
    assert torch.is_tensor(value), f"{key} must be a tensor."

    value = cast(torch.Tensor, value)
    value = rearrange(value, "n ... -> n ... 1") * onehot
    if value.ndim > 2:
        # Move the onehot to dim=1
        value = rearrange(value, "n ... t -> n t ...")
    setattr(data, f"{key}_onehot", value)


def _create_split_dataset(
    config: SplitDatasetConfig,
    task_idx: int,
    total_num_tasks: int,
    one_hot_targets: OneHotTargetsConfig,
) -> Dataset:
    # Create the dataset
    dataset_cls = registry.get_dataset_class(config.format)
    assert issubclass(dataset_cls, Dataset), f"{dataset_cls=} is not a Dataset"
    dataset = cast(Any, dataset_cls)(config.to_dict())
    dataset = cast(Dataset, dataset)

    # Wrap the dataset with task_idx transform
    def _transform(data: Data):
        nonlocal task_idx, total_num_tasks, one_hot_targets

        if not isinstance(data, Data):
            raise TypeError(f"{data=} is not a torch_geometric.data.Data")

        # Set the task_idx
        data.task_idx = torch.tensor(task_idx, dtype=torch.long)
        onehot: torch.Tensor = F.one_hot(
            data.task_idx, num_classes=total_num_tasks
        ).bool()  # (t,)
        # Set task boolean mask
        data.task_mask = rearrange(onehot, "t -> 1 t")

        # Update graph-level attrs to be a one-hot vector * attr
        for key in one_hot_targets.graph_level:
            _update_graph_value(data, key, onehot)

        # Update node-level attrs to be a one-hot vector * attr
        for key in one_hot_targets.node_level:
            _update_node_value(data, key, onehot)

        return data

    dataset = dataset_transform(dataset, _transform)
    return dataset


def _create_task_datasets(
    config: TaskDatasetConfig,
    task_idx: int,
    total_num_tasks: int,
    one_hot_targets: OneHotTargetsConfig,
):
    train_dataset = None
    val_dataset = None
    test_dataset = None

    # Create the train, val, test datasets
    if config.train is not None:
        train_dataset = _create_split_dataset(
            config.train,
            task_idx,
            total_num_tasks,
            one_hot_targets,
        )
    if config.val is not None:
        val_dataset = _create_split_dataset(
            config.val,
            task_idx,
            total_num_tasks,
            one_hot_targets,
        )
    if config.test is not None:
        test_dataset = _create_split_dataset(
            config.test,
            task_idx,
            total_num_tasks,
            one_hot_targets,
        )
    return train_dataset, val_dataset, test_dataset


def _merged_dataset(dataset_sizes_list: List[int], ratios_list: List[float]):
    dataset_sizes = np.array(dataset_sizes_list)
    ratios = np.array(ratios_list)

    # Calculate the target size of the final dataset
    target_size = sum(dataset_sizes) / sum(ratios)

    # Calculate the minimum expansion factor for each dataset
    expansion_factors = target_size * ratios / dataset_sizes

    # Make sure that the expansion factors are all at least 1.0
    expansion_factors = expansion_factors / np.min(expansion_factors)

    # Calculate the number of samples to take from each dataset
    samples_per_dataset = np.ceil(
        dataset_sizes * (expansion_factors / np.min(expansion_factors))
    ).astype(int)

    samples_per_dataset = cast(List[int], samples_per_dataset.tolist())
    return samples_per_dataset


def _combine_datasets(sampling: SamplingConfig, datasets: List[Dataset]):
    # Make sure all datasets have sizes
    dataset_sizes: List[int] = []
    for dataset in datasets:
        if not isinstance(dataset, abc.Sized):
            raise TypeError(f"{dataset=} is not a Sized")
        dataset_sizes.append(len(dataset))

    if isinstance(sampling, FullyBalancedSamplingConfig):
        ratios = [1.0] * len(dataset_sizes)
    elif isinstance(sampling, TemperatureSamplingConfig):
        total_size = sum(dataset_sizes)
        ratios = [
            (size / total_size) ** (1.0 / sampling.temperature)
            for size in dataset_sizes
        ]
    else:
        raise NotImplementedError(f"{sampling=} not implemented.")

    # Normalize the ratios
    ratios = [r / sum(ratios) for r in ratios]
    logging.info(f"Using {ratios=} for {sampling=}.")

    # Calculate the expanded dataset sizes
    expanded_dataset_sizes = _merged_dataset(dataset_sizes, ratios)

    # Expand the datasets
    expanded_datasets = [
        expand_dataset(d, n) for d, n in zip(datasets, expanded_dataset_sizes)
    ]

    # Combine the datasets
    combined_dataset = ConcatDataset(expanded_datasets)
    logging.info(
        f"Combined {len(expanded_datasets)} datasets into {len(combined_dataset)}."
    )
    return combined_dataset


def create_datasets(config: DatasetConfig):
    total_num_tasks = len(config.datasets)
    assert total_num_tasks > 0, "No tasks found in the config."

    # Create all the datasets
    train_datasets: List[Dataset] = []
    val_datasets: List[Dataset] = []
    test_datasets: List[Dataset] = []
    for task_idx, task_dataset_config in enumerate(config.datasets):
        train_dataset, val_dataset, test_dataset = _create_task_datasets(
            task_dataset_config,
            task_idx,
            total_num_tasks,
            config.one_hot_targets,
        )

        if train_dataset is not None:
            train_datasets.append(train_dataset)
        if val_dataset is not None:
            val_datasets.append(val_dataset)
        if test_dataset is not None:
            test_datasets.append(test_dataset)

    # Combine the datasets
    # For train, we need to adhere to the sampling strategy
    train_dataset = (
        _combine_datasets(config.sampling, train_datasets)
        if train_datasets
        else None
    )

    # For val and test, we just concatenate them
    val_dataset = ConcatDataset(val_datasets) if val_datasets else None
    test_dataset = ConcatDataset(test_datasets) if test_datasets else None

    return train_dataset, val_dataset, test_dataset
