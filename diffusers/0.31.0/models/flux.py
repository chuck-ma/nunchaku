import os
import types

import torch
from diffusers import FluxPipeline, FluxTransformer2DModel
from huggingface_hub import hf_hub_download
from torch import nn
from diffusers.models.embeddings import FluxPosEmbed

from .._C import QuantizedFluxModel

SVD_RANK = 32


class NunchakuFluxModel(nn.Module):
    def __init__(self, m: QuantizedFluxModel):
        super().__init__()
        self.m = m
        self.dtype = torch.bfloat16

    def forward(
        self,
        /,
        hidden_states: torch.Tensor,
        encoder_hidden_states: torch.Tensor,
        temb: torch.Tensor,
        image_rotary_emb: torch.Tensor,
        joint_attention_kwargs=None,
    ):
        batch_size = hidden_states.shape[0]
        txt_tokens = encoder_hidden_states.shape[1]
        img_tokens = hidden_states.shape[1]

        original_dtype = hidden_states.dtype

        hidden_states = hidden_states.to(self.dtype)
        encoder_hidden_states = encoder_hidden_states.to(self.dtype)
        temb = temb.to(self.dtype)

        # Get sparsity ratio from joint_attention_kwargs if provided
        sparsity_ratio = 0.0
        if joint_attention_kwargs is not None:
            sparsity_ratio = joint_attention_kwargs.get("sparsity_ratio", 0.0)

        rotary_emb_txt = image_rotary_emb[:, :txt_tokens]
        rotary_emb_img = image_rotary_emb[:, txt_tokens:]
        rotary_emb_single = image_rotary_emb

        hidden_states = self.m.forward(
            hidden_states,
            encoder_hidden_states,
            temb,
            rotary_emb_img,
            rotary_emb_txt,
            rotary_emb_single,
            sparsity_ratio,
        )

        hidden_states = hidden_states.to(original_dtype)

        encoder_hidden_states = hidden_states[:, :txt_tokens, ...]
        hidden_states = hidden_states[:, txt_tokens:, ...]

        return encoder_hidden_states, hidden_states


def load_quantized_model(path: str, device: str | torch.device) -> QuantizedFluxModel:
    device = torch.device(device)
    assert device.type == "cuda"

    m = QuantizedFluxModel()
    m.disableMemoryAutoRelease()
    m.init(True, 0 if device.index is None else device.index)
    m.load(path)
    return m


def inject_pipeline(pipe: FluxPipeline, m: QuantizedFluxModel) -> FluxPipeline:
    net: FluxTransformer2DModel = pipe.transformer
    # 使用原生的FluxPosEmbed，不再需要自定义的EmbedND
    net.transformer_blocks = torch.nn.ModuleList([NunchakuFluxModel(m)])
    net.single_transformer_blocks = torch.nn.ModuleList([])

    def update_params(self: FluxTransformer2DModel, path: str):
        if not os.path.exists(path):
            hf_repo_id = os.path.dirname(path)
            filename = os.path.basename(path)
            path = hf_hub_download(repo_id=hf_repo_id, filename=filename)
        block = self.transformer_blocks[0]
        assert isinstance(block, NunchakuFluxModel)
        block.m.load(path, True)

    def set_lora_scale(self: FluxTransformer2DModel, scale: float):
        block = self.transformer_blocks[0]
        assert isinstance(block, NunchakuFluxModel)
        block.m.setLoraScale(SVD_RANK, scale)

    net.nunchaku_update_params = types.MethodType(update_params, net)
    net.nunchaku_set_lora_scale = types.MethodType(set_lora_scale, net)

    return pipe


def inject_transformer(
    transformer_model: FluxTransformer2DModel, m: QuantizedFluxModel
) -> None:
    """注入自定义transformer模型

    Args:
        transformer_model: 原始transformer模型
        custom_model: 要注入的自定义模型
    """
    transformer_model.pos_embed = FluxPosEmbed(theta=10000, axes_dim=[16, 56, 56])

    # 替换transformer块
    transformer_model.transformer_blocks = torch.nn.ModuleList([NunchakuFluxModel(m)])
    transformer_model.single_transformer_blocks = torch.nn.ModuleList([])

    def update_params(self: FluxTransformer2DModel, path: str):
        if not os.path.exists(path):
            hf_repo_id = os.path.dirname(path)
            filename = os.path.basename(path)
            path = hf_hub_download(repo_id=hf_repo_id, filename=filename)
        block = self.transformer_blocks[0]
        assert isinstance(block, NunchakuFluxModel)
        block.m.load(path, True)

    def set_lora_scale(self: FluxTransformer2DModel, scale: float):
        block = self.transformer_blocks[0]
        assert isinstance(block, NunchakuFluxModel)
        block.m.setLoraScale(SVD_RANK, scale)

    transformer_model.nunchaku_update_params = types.MethodType(
        update_params, transformer_model
    )
    transformer_model.nunchaku_set_lora_scale = types.MethodType(
        set_lora_scale, transformer_model
    )

    return transformer_model