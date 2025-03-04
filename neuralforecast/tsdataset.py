# AUTOGENERATED! DO NOT EDIT! File to edit: ../nbs/tsdataset.ipynb.

# %% auto 0
__all__ = ['TimeSeriesLoader', 'TimeSeriesDataset', 'TimeSeriesDataModule']

# %% ../nbs/tsdataset.ipynb 4
import warnings
from collections.abc import Mapping

import numpy as np
import pandas as pd
import pytorch_lightning as pl
import torch
import utilsforecast.processing as ufp
from torch.utils.data import Dataset, DataLoader
from utilsforecast.compat import DataFrame, pl_Series

# %% ../nbs/tsdataset.ipynb 5
class TimeSeriesLoader(DataLoader):
    """TimeSeriesLoader DataLoader.
    [Source code](https://github.com/Nixtla/neuralforecast1/blob/main/neuralforecast/tsdataset.py).

    Small change to PyTorch's Data loader.
    Combines a dataset and a sampler, and provides an iterable over the given dataset.

    The class `~torch.utils.data.DataLoader` supports both map-style and
    iterable-style datasets with single- or multi-process loading, customizing
    loading order and optional automatic batching (collation) and memory pinning.

    **Parameters:**<br>
    `batch_size`: (int, optional): how many samples per batch to load (default: 1).<br>
    `shuffle`: (bool, optional): set to `True` to have the data reshuffled at every epoch (default: `False`).<br>
    `sampler`: (Sampler or Iterable, optional): defines the strategy to draw samples from the dataset.<br>
                Can be any `Iterable` with `__len__` implemented. If specified, `shuffle` must not be specified.<br>
    """

    def __init__(self, dataset, **kwargs):
        if "collate_fn" in kwargs:
            kwargs.pop("collate_fn")
        kwargs_ = {**kwargs, **dict(collate_fn=self._collate_fn)}
        DataLoader.__init__(self, dataset=dataset, **kwargs_)

    def _collate_fn(self, batch):
        elem = batch[0]
        elem_type = type(elem)

        if isinstance(elem, torch.Tensor):
            out = None
            if torch.utils.data.get_worker_info() is not None:
                # If we're in a background process, concatenate directly into a
                # shared memory tensor to avoid an extra copy
                numel = sum(x.numel() for x in batch)
                storage = elem.storage()._new_shared(numel, device=elem.device)
                out = elem.new(storage).resize_(len(batch), *list(elem.size()))
            return torch.stack(batch, 0, out=out)

        elif isinstance(elem, Mapping):
            if elem["static"] is None:
                return dict(
                    temporal=self.collate_fn([d["temporal"] for d in batch]),
                    temporal_cols=elem["temporal_cols"],
                )

            return dict(
                static=self.collate_fn([d["static"] for d in batch]),
                static_cols=elem["static_cols"],
                temporal=self.collate_fn([d["temporal"] for d in batch]),
                temporal_cols=elem["temporal_cols"],
            )

        raise TypeError(f"Unknown {elem_type}")

# %% ../nbs/tsdataset.ipynb 7
class TimeSeriesDataset(Dataset):
    def __init__(
        self,
        temporal,
        temporal_cols,
        indptr,
        max_size: int,
        min_size: int,
        static=None,
        static_cols=None,
        sorted=False,
    ):
        super().__init__()
        self.temporal = torch.tensor(temporal, dtype=torch.float)
        self.temporal_cols = pd.Index(list(temporal_cols))

        if static is not None:
            self.static = torch.tensor(static, dtype=torch.float)
            self.static_cols = static_cols
        else:
            self.static = static
            self.static_cols = static_cols

        self.indptr = indptr
        self.n_groups = self.indptr.size - 1
        self.max_size = max_size
        self.min_size = min_size

        # Upadated flag. To protect consistency, dataset can only be updated once
        self.updated = False
        self.sorted = sorted

    def __getitem__(self, idx):
        if isinstance(idx, int):
            # Parse temporal data and pad its left
            temporal = torch.zeros(
                size=(len(self.temporal_cols), self.max_size), dtype=torch.float32
            )
            ts = self.temporal[self.indptr[idx] : self.indptr[idx + 1], :]
            temporal[: len(self.temporal_cols), -len(ts) :] = ts.permute(1, 0)

            # Add static data if available
            static = None if self.static is None else self.static[idx, :]

            item = dict(
                temporal=temporal,
                temporal_cols=self.temporal_cols,
                static=static,
                static_cols=self.static_cols,
            )

            return item
        raise ValueError(f"idx must be int, got {type(idx)}")

    def __len__(self):
        return self.n_groups

    def __repr__(self):
        return f"TimeSeriesDataset(n_data={self.temporal.shape[0]:,}, n_groups={self.n_groups:,})"

    def __eq__(self, other):
        if not hasattr(other, "data") or not hasattr(other, "indptr"):
            return False
        return np.allclose(self.data, other.data) and np.array_equal(
            self.indptr, other.indptr
        )

    def align(self, df: DataFrame) -> "TimeSeriesDataset":
        # Protect consistency
        df = ufp.copy_if_pandas(df, deep=False)

        # Add Nones to missing columns (without available_mask)
        temporal_cols = self.temporal_cols.copy()
        for col in temporal_cols:
            if col not in df.columns:
                df = ufp.assign_columns(df, col, np.nan)
            if col == "available_mask":
                df = ufp.assign_columns(df, col, 1.0)

        # Sort columns to match self.temporal_cols (without available_mask)
        df = df[["unique_id", "ds"] + temporal_cols.tolist()]

        # Process future_df
        dataset, *_ = TimeSeriesDataset.from_df(df=df, sort_df=self.sorted)
        return dataset

    def append(self, futr_dataset: "TimeSeriesDataset") -> "TimeSeriesDataset":
        """Add future observations to the dataset. Returns a copy"""
        if self.indptr.size != futr_dataset.indptr.size:
            raise ValueError(
                "Cannot append `futr_dataset` with different number of groups."
            )
        # Define and fill new temporal with updated information
        len_temporal, col_temporal = self.temporal.shape
        len_futr = futr_dataset.temporal.shape[0]
        new_temporal = torch.empty(size=(len_temporal + len_futr, col_temporal))
        new_sizes = np.diff(self.indptr) + np.diff(futr_dataset.indptr)
        new_indptr = np.append(0, new_sizes.cumsum()).astype(np.int32)
        new_max_size = np.max(new_sizes)

        for i in range(self.n_groups):
            curr_slice = slice(self.indptr[i], self.indptr[i + 1])
            curr_size = curr_slice.stop - curr_slice.start
            futr_slice = slice(futr_dataset.indptr[i], futr_dataset.indptr[i + 1])
            new_temporal[new_indptr[i] : new_indptr[i] + curr_size] = self.temporal[
                curr_slice
            ]
            new_temporal[
                new_indptr[i] + curr_size : new_indptr[i + 1]
            ] = futr_dataset.temporal[futr_slice]

        # Define new dataset
        updated_dataset = TimeSeriesDataset(
            temporal=new_temporal,
            temporal_cols=self.temporal_cols.copy(),
            indptr=new_indptr,
            max_size=new_max_size,
            min_size=self.min_size,
            static=self.static,
            static_cols=self.static_cols,
            sorted=self.sorted,
        )

        return updated_dataset

    @staticmethod
    def update_dataset(dataset, futr_df):
        futr_dataset = dataset.align(futr_df)
        return dataset.append(futr_dataset)

    @staticmethod
    def trim_dataset(dataset, left_trim: int = 0, right_trim: int = 0):
        """
        Trim temporal information from a dataset.
        Returns temporal indexes [t+left:t-right] for all series.
        """
        if dataset.min_size <= left_trim + right_trim:
            raise Exception(
                f"left_trim + right_trim ({left_trim} + {right_trim}) \
                                must be lower than the shorter time series ({dataset.min_size})"
            )

        # Define and fill new temporal with trimmed information
        len_temporal, col_temporal = dataset.temporal.shape
        total_trim = (left_trim + right_trim) * dataset.n_groups
        new_temporal = torch.zeros(size=(len_temporal - total_trim, col_temporal))
        new_indptr = [0]

        acum = 0
        for i in range(dataset.n_groups):
            series_length = dataset.indptr[i + 1] - dataset.indptr[i]
            new_length = series_length - left_trim - right_trim
            new_temporal[acum : (acum + new_length), :] = dataset.temporal[
                dataset.indptr[i] + left_trim : dataset.indptr[i + 1] - right_trim, :
            ]
            acum += new_length
            new_indptr.append(acum)

        new_max_size = dataset.max_size - left_trim - right_trim
        new_min_size = dataset.min_size - left_trim - right_trim

        # Define new dataset
        updated_dataset = TimeSeriesDataset(
            temporal=new_temporal,
            temporal_cols=dataset.temporal_cols.copy(),
            indptr=np.array(new_indptr).astype(np.int32),
            max_size=new_max_size,
            min_size=new_min_size,
            static=dataset.static,
            static_cols=dataset.static_cols,
            sorted=dataset.sorted,
        )

        return updated_dataset

    @staticmethod
    def from_df(df, static_df=None, sort_df=False):
        # TODO: protect on equality of static_df + df indexes
        if isinstance(df, pd.DataFrame) and df.index.name == "unique_id":
            warnings.warn(
                "Passing the id as index is deprecated, please provide it as a column instead.",
                DeprecationWarning,
            )
            df = df.reset_index("unique_id")
        # Define indexes if not given
        if static_df is not None:
            if (
                isinstance(static_df, pd.DataFrame)
                and static_df.index.name == "unique_id"
            ):
                warnings.warn(
                    "Passing the id as index is deprecated, please provide it as a column instead.",
                    DeprecationWarning,
                )
            if sort_df:
                static_df = ufp.sort(static_df, by="unique_id")

        ids, times, data, indptr, sort_idxs = ufp.process_df(df, "unique_id", "ds", "y")
        # processor sets y as the first column
        temporal_cols = pd.Index(
            ["y"] + [c for c in df.columns if c not in ("unique_id", "ds", "y")]
        )
        temporal = data.astype(np.float32, copy=False)
        indices = ids
        if isinstance(df, pd.DataFrame):
            dates = pd.Index(times, name="ds")
        else:
            dates = pl_Series("ds", times)
        sizes = np.diff(indptr)
        max_size = max(sizes)
        min_size = min(sizes)

        # Add Available mask efficiently (without adding column to df)
        if "available_mask" not in df.columns:
            available_mask = np.ones((len(temporal), 1), dtype=np.float32)
            temporal = np.append(temporal, available_mask, axis=1)
            temporal_cols = temporal_cols.append(pd.Index(["available_mask"]))

        # Static features
        if static_df is not None:
            static_cols = static_df.columns.drop("unique_id")
            static = ufp.to_numpy(static_df[static_cols])
        else:
            static = None
            static_cols = None

        dataset = TimeSeriesDataset(
            temporal=temporal,
            temporal_cols=temporal_cols,
            static=static,
            static_cols=static_cols,
            indptr=indptr,
            max_size=max_size,
            min_size=min_size,
            sorted=sort_df,
        )
        ds = df["ds"].to_numpy()
        if sort_idxs is not None:
            ds = ds[sort_idxs]
        return dataset, indices, dates, ds

# %% ../nbs/tsdataset.ipynb 10
class TimeSeriesDataModule(pl.LightningDataModule):
    def __init__(
        self,
        dataset: TimeSeriesDataset,
        batch_size=32,
        valid_batch_size=1024,
        num_workers=0,
        drop_last=False,
    ):
        super().__init__()
        self.dataset = dataset
        self.batch_size = batch_size
        self.valid_batch_size = valid_batch_size
        self.num_workers = num_workers
        self.drop_last = drop_last

    def train_dataloader(self):
        loader = TimeSeriesLoader(
            self.dataset,
            batch_size=self.batch_size,
            num_workers=self.num_workers,
            shuffle=True,
            drop_last=self.drop_last,
        )
        return loader

    def val_dataloader(self):
        loader = TimeSeriesLoader(
            self.dataset,
            batch_size=self.valid_batch_size,
            num_workers=self.num_workers,
            shuffle=False,
            drop_last=self.drop_last,
        )
        return loader

    def predict_dataloader(self):
        loader = TimeSeriesLoader(
            self.dataset,
            batch_size=self.valid_batch_size,
            num_workers=self.num_workers,
            shuffle=False,
        )
        return loader
