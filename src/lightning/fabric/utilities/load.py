# Copyright 2023 MathInf GmbH
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this files from this repository except in compliance
# with the License reproduced below (also at
# http://www.apache.org/licenses/LICENSE-2.0).
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import pickle
import warnings
from functools import partial
from io import BytesIO
from typing import Any, Callable, Dict, IO, Optional, Sequence

import torch
import torch.utils._device
from torch import Tensor
from torch.storage import TypedStorage, UntypedStorage

from lightning.fabric.utilities.types import _PATH


# Modified from https://github.com/lernapparat/torchhacks by Thomas Viehmann
class _NotYetLoadedTensor:
    def __init__(self, metatensor, archiveinfo, storageinfo, rebuild_args):
        self.metatensor = metatensor
        self.archiveinfo = archiveinfo
        self.storageinfo = storageinfo
        self.rebuild_args = rebuild_args

    @classmethod
    def rebuild_from_type_v2(cls, func, new_type, args, state, *, archiveinfo=None):
        ret = func(*args)
        if isinstance(ret, _NotYetLoadedTensor):
            old_lt = ret._load_tensor

            def _load_tensor():
                t = old_lt()
                return torch._tensor._rebuild_from_type_v2(lambda: t, new_type, (), state)

            ret._load_tensor = _load_tensor
            return ret
        return torch._tensor._rebuild_from_type_v2(func, new_type, args, state)

    @classmethod
    def rebuild_parameter(cls, data, requires_grad, backward_hooks, *, archiveinfo=None):
        if isinstance(data, _NotYetLoadedTensor):
            old_lt = data._load_tensor

            def _load_tensor():
                t = old_lt()
                return torch._utils._rebuild_parameter(t, requires_grad, backward_hooks)

            data._load_tensor = _load_tensor
            return data
        return torch._utils._rebuild_parameter(data, requires_grad, backward_hooks)

    @classmethod
    def rebuild_tensor_v2(
        cls, storage, storage_offset, size, stride, requires_grad, backward_hooks, metadata=None, *, archiveinfo=None
    ):
        rebuild_args = (storage_offset, size, stride, requires_grad, backward_hooks, metadata)
        metatensor = torch._utils._rebuild_tensor_v2(
            storage, storage_offset, size, stride, requires_grad, backward_hooks, metadata
        )
        storageinfo = storage.archiveinfo
        return _NotYetLoadedTensor(metatensor, archiveinfo, storageinfo, rebuild_args)

    def _load_tensor(self) -> Tensor:
        name, storage_cls, fn, device, size = self.storageinfo
        dtype = self.metatensor.dtype

        uts = (
            self.archiveinfo.file_reader.get_storage_from_record(
                f"data/{fn}", size * torch._utils._element_size(dtype), UntypedStorage
            )
            ._typed_storage()
            ._untyped_storage
        )
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            storage = TypedStorage(wrap_storage=uts, dtype=self.metatensor.dtype, _internal=True)
        return torch._utils._rebuild_tensor_v2(storage, *self.rebuild_args)

    @classmethod
    def __torch_function__(
        cls,
        func: Callable,
        types: Sequence,
        args: Sequence[Any] = (),
        kwargs: Optional[Dict] = None,
    ) -> Any:
        kwargs = kwargs or {}
        loaded_args = [(arg._load_tensor() if isinstance(arg, _NotYetLoadedTensor) else arg) for arg in args]
        return func(*loaded_args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        # These properties don't require materialization and can be accessed through the meta tensor directly
        if name in {
            "dtype",
            "grad",
            "grad_fn",
            "layout",
            "names",
            "ndim",
            "output_nr",
            "requires_grad",
            "retains_grad",
            "size",
            "shape",
            "volatile",
        }:
            return getattr(self.metatensor, name)

        # TODO: needed for us?
        # materializing with contiguous is needed for quantization
        if name in {"contiguous"}:
            return getattr(self._load_tensor(), name)

        raise AttributeError(f"{type(self)} does not have {name}")

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({repr(self.metatensor)})"


class _LazyLoadingUnpickler(pickle.Unpickler):
    def __init__(self, file: IO, file_reader: torch.PyTorchFileReader) -> None:
        super().__init__(file)
        self.file_reader = file_reader

    def find_class(self, module: str, name: str) -> Any:
        if module == "torch._utils" and name == "_rebuild_tensor_v2":
            return partial(_NotYetLoadedTensor.rebuild_tensor_v2, archiveinfo=self)
        if module == "torch._tensor" and name == "_rebuild_from_type_v2":
            return partial(_NotYetLoadedTensor.rebuild_from_type_v2, archiveinfo=self)
        if module == "torch._utils" and name == "_rebuild_parameter":
            return partial(_NotYetLoadedTensor.rebuild_parameter, archiveinfo=self)
        return super().find_class(module, name)

    def persistent_load(self, pid) -> TypedStorage:
        name, cls, fn, device, size = pid
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # TODO: needed?
            storage = TypedStorage(dtype=cls().dtype, device="meta")
        storage.archiveinfo = pid
        return storage


# @contextmanager
# def _lazy_load(filename: _PATH) -> Any:
#     file_reader = torch.PyTorchFileReader(str(filename))
#     with BytesIO(file_reader.get_record("data.pkl")) as pkl:
#         mup = _LazyLoadingUnpickler(pkl, file_reader)
#         yield mup.load()


def _lazy_load(filename: _PATH) -> Any:
    file_reader = torch.PyTorchFileReader(str(filename))
    with BytesIO(file_reader.get_record("data.pkl")) as pkl:
        mup = _LazyLoadingUnpickler(pkl, file_reader)
        return mup.load()


def _materialize_tensors(checkpoint: Dict[str, Any]) -> None:
    for k, v in checkpoint.items():
        if isinstance(v, _NotYetLoadedTensor):
            checkpoint[k] = v._load_tensor()
