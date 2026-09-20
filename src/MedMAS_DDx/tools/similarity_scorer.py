

from __future__ import annotations

import logging
from typing import Sequence

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "cambridgeltl/SapBERT-from-PubMedBERT-fulltext"

DEFAULT_THRESHOLD = 0.6

_MAX_TOKEN_LENGTH = 64


class DiagnosisSimilarityScorer:
  

    def __init__(
        self,
        model_name: str = DEFAULT_MODEL,
        threshold: float = DEFAULT_THRESHOLD,
    ) -> None:
        self.model_name = model_name
        self.threshold = threshold
        self._tokenizer = None
        self._model = None
        self._torch = None


    def _ensure_loaded(self) -> None:
        """Load the tokenizer and model on first use (idempotent)."""
        if self._model is not None:
            return
        try:
            import torch
            from transformers import AutoModel, AutoTokenizer
        except ImportError as exc:
            raise ImportError(
                "transformers and torch are required for SapBERT similarity "
                "scoring.\n"
                "Install them with:  uv add transformers torch\n"
                "or:                 pip install transformers torch"
            ) from exc

        logger.info("Loading SapBERT model: %s", self.model_name)
        self._tokenizer = AutoTokenizer.from_pretrained(self.model_name)
        self._model = AutoModel.from_pretrained(self.model_name)
        self._model.eval() 
        self._torch = torch


    def encode(self, texts: list[str]):
      
        self._ensure_loaded()
        torch = self._torch
        assert torch is not None  
        with torch.no_grad():
            batch = self._tokenizer(
                texts,
                padding=True,
                truncation=True,
                max_length=_MAX_TOKEN_LENGTH,
                return_tensors="pt",
            )
            outputs = self._model(**batch)
           
            cls = outputs.last_hidden_state[:, 0, :]
            cls = torch.nn.functional.normalize(cls, p=2, dim=1)
        return cls.cpu().numpy()

    def score_ranking(
        self,
        ranking: Sequence[str],
        expected: str,
    ) -> list[float]:
      
        if not ranking or not expected:
            return [0.0] * len(ranking)
        import numpy as np

        texts = list(ranking) + [expected.strip()]
        vecs = self.encode(texts)
        expected_vec = vecs[-1]
        return [float(np.dot(vec, expected_vec)) for vec in vecs[:-1]]



_default_scorer: DiagnosisSimilarityScorer | None = None


def get_default_scorer(
    model_name: str = DEFAULT_MODEL,
    threshold: float = DEFAULT_THRESHOLD,
) -> DiagnosisSimilarityScorer:
 
    global _default_scorer
    if _default_scorer is None or _default_scorer.model_name != model_name:
        _default_scorer = DiagnosisSimilarityScorer(
            model_name=model_name,
            threshold=threshold,
        )
    return _default_scorer
