"""
Embedding 编码服务 — BAAI/bge-m3 本地推理

模型：BAAI/bge-m3（多语言，1024维，中文支持好）
推理：sentence-transformers 本地运行，无需外部 API
首次运行自动下载模型（~1.2GB），缓存在 ~/.cache/huggingface/
"""

import os
import sys
import time

# 延迟导入，避免启动时加载大模型
_model = None
_model_version = "bge-m3"

EMBEDDING_DIM = 1024


def _load_model():
    """延迟加载 bge-m3 模型（首次调用时加载，约 3-5s）"""
    global _model
    if _model is not None:
        return _model

    try:
        from sentence_transformers import SentenceTransformer
        print("[Impact:Embed] 正在加载 bge-m3 模型...", flush=True)
        _model = SentenceTransformer(
            "BAAI/bge-m3",
            device="cpu",  # CPU 足够，避免 GPU 依赖
        )
        print("[Impact:Embed] bge-m3 模型加载完成（dim=%d）" % _model.get_sentence_embedding_dimension(), flush=True)
        return _model
    except ImportError:
        print("[Impact:Embed] sentence-transformers 未安装，embedding 不可用", flush=True)
        print("  安装命令: pip install sentence-transformers", flush=True)
        return None
    except Exception as e:
        print("[Impact:Embed] 模型加载失败: %s" % e, flush=True)
        return None


def encode_single(text):
    """编码单条文本，返回 float32 numpy array (1024,)

    输入：新闻 title + brief 拼接
    输出：1024维 float32 向量
    """
    model = _load_model()
    if model is None:
        return None

    try:
        vec = model.encode(text, normalize_embeddings=True, show_progress_bar=False)
        return vec
    except Exception as e:
        print("[Impact:Embed] 编码失败: %s" % e, flush=True)
        return None


def encode_batch(texts, batch_size=32, show_progress=True):
    """批量编码文本列表

    Args:
        texts: List[str] — 待编码文本列表
        batch_size: int — 批次大小
        show_progress: bool — 是否显示进度条

    Returns:
        numpy array (N, 1024) 或 None
    """
    model = _load_model()
    if model is None:
        return None

    try:
        vecs = model.encode(
            texts,
            batch_size=batch_size,
            normalize_embeddings=True,
            show_progress_bar=show_progress and len(texts) > 10,
        )
        return vecs
    except Exception as e:
        print("[Impact:Embed] 批量编码失败: %s" % e, flush=True)
        return None


def is_available():
    """检查 embedding 服务是否可用"""
    return _load_model() is not None


def get_model_info():
    """返回模型信息"""
    return {
        "model": "BAAI/bge-m3",
        "version": _model_version,
        "dim": EMBEDDING_DIM,
        "loaded": _model is not None,
    }
