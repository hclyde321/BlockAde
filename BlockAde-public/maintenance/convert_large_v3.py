"""Convert the verified OpenAI Large v3 checkpoint to whisper.cpp FP16 GGML.

Uses the binary layout in whisper.cpp/models/convert-pt-to-ggml.py. This limited
reader accepts only the globals/storage type present in the official checkpoint,
not arbitrary PyTorch pickle objects. No PyTorch installation is needed.
"""
from __future__ import annotations

import argparse
import ast
import base64
from collections import OrderedDict
import hashlib
import io
import json
from pathlib import Path
import pickle
import struct
import sys
import zipfile

import numpy as np

SOURCE_SHA256 = 'e5b1a55b89c1367dacf97e3e19bfd829a01529dbfdeefa8caeb59b3f1b81dadb'
# Cross-checked against ggerganov/whisper.cpp ggml-large-v3.bin LFS SHA-256.
GGML_SHA256 = '64d182b440b98d5203c4f9bd541544d84c605196c4f7b845dfa11fb23594d1e2'
DIMENSIONS = ('n_vocab', 'n_audio_ctx', 'n_audio_state', 'n_audio_head', 'n_audio_layer',
              'n_text_ctx', 'n_text_state', 'n_text_head', 'n_text_layer', 'n_mels')
EXPECTED_DIMS = (51866, 1500, 1280, 20, 32, 448, 1280, 20, 32, 128)


class HalfStorage:
    pass


def rebuild_tensor(storage, offset, shape, stride, requires_grad, hooks):
    del requires_grad, hooks
    return storage, offset, tuple(shape), tuple(stride)


class CheckpointReader(pickle.Unpickler):
    def find_class(self, module, name):
        allowed = {('collections', 'OrderedDict'): OrderedDict,
                   ('torch', 'HalfStorage'): HalfStorage,
                   ('torch._utils', '_rebuild_tensor_v2'): rebuild_tensor}
        if (module, name) not in allowed:
            raise ValueError(f'Unsupported checkpoint type: {module}.{name}')
        return allowed[module, name]

    def persistent_load(self, pid):
        kind, dtype, key, location, size = pid
        if kind != 'storage' or dtype is not HalfStorage or not str(key).isdigit():
            raise ValueError('Unsupported checkpoint storage')
        return str(key), int(size)


def digest(path):
    with Path(path).open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def mel_data(path):
    with zipfile.ZipFile(path) as archive:
        data = archive.read('mel_128.npy')
    if data[:8] != b'\x93NUMPY\x01\x00':
        raise ValueError('Expected NumPy v1 mel filters')
    length = struct.unpack('<H', data[8:10])[0]
    header = ast.literal_eval(data[10:10 + length].decode('latin1'))
    if header['descr'] != '<f4' or header['fortran_order'] or header['shape'] != (128, 201):
        raise ValueError('Unexpected mel filter layout')
    values = data[10 + length:]
    if len(values) != 128 * 201 * 4:
        raise ValueError('Truncated mel filters')
    return values


def convert(source: Path, assets: Path, destination: Path):
    if sys.byteorder != 'little':
        raise ValueError('This converter requires a little-endian host')
    if destination.exists():
        raise FileExistsError(f'Refusing to replace existing model: {destination}')
    print('Verifying official checkpoint SHA-256...', flush=True)
    if digest(source) != SOURCE_SHA256:
        raise ValueError('Checkpoint SHA-256 does not match official Large v3')
    partial = destination.with_suffix(destination.suffix + '.part')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(source) as archive:
        checkpoint = CheckpointReader(io.BytesIO(archive.read('archive/data.pkl'))).load()
        dims = tuple(checkpoint['dims'][key] for key in DIMENSIONS)
        if dims != EXPECTED_DIMS:
            raise ValueError(f'Unexpected model dimensions: {dims}')
        weights = checkpoint['model_state_dict']
        with partial.open('wb') as output:
            def integers(*values):
                output.write(struct.pack('<' + 'i' * len(values), *values))
            integers(0x67676d6c, *dims, 1)  # ftype 1: unquantized FP16
            integers(128, 201)
            output.write(mel_data(assets / 'mel_filters.npz'))
            tokens = [base64.b64decode(line.split()[0]) for line in
                      (assets / 'multilingual.tiktoken').read_bytes().splitlines() if line]
            integers(len(tokens))
            for token in tokens:
                integers(len(token))
                output.write(token)
            for index, (name, tensor) in enumerate(weights.items()):
                (key, storage_size), offset, original_shape, stride = tensor
                shape = original_shape
                shape = tuple(size for size in shape if size != 1)
                if name in ('encoder.conv1.bias', 'encoder.conv2.bias'):
                    shape = (shape[0], 1)
                raw = archive.read(f'archive/data/{key}')
                if len(raw) != storage_size * 2 or offset < 0:
                    raise ValueError(f'Invalid storage: {name}')
                values = np.ndarray(original_shape, dtype="<f2", buffer=raw,
                                    offset=offset * 2, strides=tuple(step * 2 for step in stride))
                use_f32 = len(shape) < 2 or name in (
                    'encoder.conv1.bias', 'encoder.conv2.bias',
                    'encoder.positional_embedding', 'decoder.positional_embedding')
                raw = values.astype("<f4" if use_f32 else "<f2").tobytes(order="C")
                encoded = name.encode('utf-8')
                integers(len(shape), len(encoded), 0 if use_f32 else 1)
                integers(*reversed(shape))
                output.write(encoded)
                output.write(raw)
                if index % 100 == 0:
                    print(f'Converted {index + 1}/{len(weights)} tensors', flush=True)
    output_hash = digest(partial)
    if output_hash != GGML_SHA256:
        raise ValueError('Converted model differs from the published whisper.cpp FP16 model')
    partial.rename(destination)
    metadata = {'source': str(source), 'source_sha256': SOURCE_SHA256,
                'ggml_sha256': output_hash, 'format': 'GGML FP16 (unquantized)',
                'dimensions': dict(zip(DIMENSIONS, dims)), 'tensors': len(weights),
                'assets_sha256': {name: digest(assets / name) for name in
                                 ('mel_filters.npz', 'multilingual.tiktoken')}}
    destination.with_suffix('.conversion.json').write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'Complete: {destination}\nSHA-256: {output_hash}', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('assets', type=Path)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    convert(args.source, args.assets, args.destination)
