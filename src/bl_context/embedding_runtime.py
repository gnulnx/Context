"""Heavy embedding runtime loaded only for model verification and indexing."""

import argparse
from pathlib import Path

import numpy as np
from fastembed import TextEmbedding

from .embedding import DIMENSIONS, MODEL


def load_files(directory):
    return TextEmbedding(
        model_name=MODEL,
        cache_dir=str(directory),
        specific_model_path=str(directory),
        local_files_only=True,
        threads=2,
        cuda=False,
    )


def probe(model):
    documents = [
        "The rover uses lithium batteries for electrical power.",
        "The web interface has a purple navigation sidebar.",
        "Database backups run nightly and retain seven snapshots.",
    ]
    queries = [
        "rover electrical power batteries",
        "website navigation appearance",
        "nightly database backup retention",
    ]
    passages = np.array(list(model.passage_embed(documents)))
    query_vectors = np.array(list(model.query_embed(queries)))
    for values in (passages, query_vectors):
        valid_shape = values.shape == (3, DIMENSIONS)
        finite = np.isfinite(values).all()
        nonzero = (np.linalg.norm(values, axis=1) > 0).all() if valid_shape else False
        if not valid_shape or not finite or not nonzero:
            raise RuntimeError(
                "Embedding probe returned invalid shape, nonfinite values, or zero vectors."
            )
    if not np.array_equal((query_vectors @ passages.T).argmax(axis=1), np.arange(3)):
        raise RuntimeError("Embedding retrieval probe failed.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    probe(load_files(args.directory))


if __name__ == "__main__":
    main()
