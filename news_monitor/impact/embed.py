"""
Embedding 编码服务 — BAAI/bge-m3 本地推理

模型：BAAI/bge-m3（多语言，1024维，中文支持好）
推理：transformers + safetensors 本地运行，无需外部 API
首次运行自动下载模型（~2GB），缓存在 ~/.cache/huggingface/
"""

import os
import sys
import time

import numpy as np

# 延迟导入，避免启动时加载大模型
_model = None
_tokenizer = None
_model_version = "bge-m3"

EMBEDDING_DIM = 1024


def _load_model():
    """延迟加载 bge-m3 模型（首次调用时加载，约 3-5s）"""
    global _model, _tokenizer
    if _model is not None:
        return _model

    import socket
    socket.setdefaulttimeout(10)  # 10秒超时，避免 SSL 重试卡住

    try:
        import torch
        from transformers import AutoTokenizer, AutoModel

        print("[Impact:Embed] 正在加载 bge-m3 模型...", flush=True)
        _tokenizer = AutoTokenizer.from_pretrained("BAAI/bge-m3", timeout=10)
        _model = AutoModel.from_pretrained("BAAI/bge-m3", use_safetensors=True, timeout=10)
        _model.eval()
        print("[Impact:Embed] bge-m3 模型加载完成（dim=%d）" % EMBEDDING_DIM, flush=True)
        return _model
    except ImportError:
        print("[Impact:Embed] transformers 未安装，embedding 不可用", flush=True)
        print("  安装命令: pip install transformers torch", flush=True)
        return None
    except Exception as e:
        print("[Impact:Embed] 模型加载失败: %s" % e, flush=True)
        return None


def _encode(texts, show_progress=False):
    """内部编码函数，返回 normalized numpy array"""
    import torch

    model = _load_model()
    if model is None:
        return None

    try:
        inputs = _tokenizer(
            texts if isinstance(texts, list) else [texts],
            return_tensors="pt",
            padding=True,
            truncation=True,
            max_length=512,
        )
        with torch.no_grad():
            outputs = model(**inputs)
            # CLS token embedding
            vecs = outputs.last_hidden_state[:, 0, :]
            vecs = torch.nn.functional.normalize(vecs, p=2, dim=1)
        return vecs.numpy()
    except Exception as e:
        print("[Impact:Embed] 编码失败: %s" % e, flush=True)
        return None


def encode_single(text):
    """编码单条文本，返回 float32 numpy array (1024,)

    输入：新闻 title + brief 拼接
    输出：1024维 float32 向量
    """
    result = _encode(text)
    if result is None:
        return None
    return result[0]


def encode_batch(texts, batch_size=32, show_progress=True):
    """批量编码文本列表

    Args:
        texts: List[str] — 待编码文本列表
        batch_size: int — 批次大小
        show_progress: bool — 是否显示进度

    Returns:
        numpy array (N, 1024) 或 None
    """
    model = _load_model()
    if model is None:
        return None

    try:
        all_vecs = []
        total = len(texts)
        for i in range(0, total, batch_size):
            batch = texts[i:i + batch_size]
            vecs = _encode(batch)
            if vecs is None:
                return None
            all_vecs.append(vecs)
            if show_progress and total > 10:
                print("[Impact:Embed] 编码进度: %d/%d" % (min(i + batch_size, total), total), flush=True)
        return np.vstack(all_vecs)
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
