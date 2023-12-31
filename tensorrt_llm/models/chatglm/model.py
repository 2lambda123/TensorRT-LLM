# SPDX-FileCopyrightText: Copyright (c) 2022-2023 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import argparse

import tensorrt as trt

from ..._common import default_net
from ..._utils import pad_vocab_size, str_dtype_to_trt
from ...functional import (PositionEmbeddingType, Tensor, concat,
                           gather_last_token_logits, shape)
from ...layers import (MLP, Attention, AttentionMaskType, AttentionParams,
                       ColumnLinear, Embedding, KeyValueCacheParams, LayerNorm,
                       RmsNorm)
from ...module import Module, ModuleList
from ..generation_mixin import GenerationMixin


class ChatGLMDecoderLayer(Module):

    def __init__(self, layer_id, args):

        super().__init__()

        self.model_name = args.model_name
        self.use_cache = args.use_cache
        rotary_embedding_scaling = None

        if self.model_name in ["chatglm_6b"]:
            self.alpha = (2 * args.num_layers)**0.5
            self.norm = LayerNorm
            attention_mask_type = AttentionMaskType.bidirectional
            position_embedding_type = PositionEmbeddingType.chatglm
        elif args.model_name in [
                "chatglm2_6b", "chatglm2_6b_32k", "chatglm3_6b",
                "chatglm3_6b_base", "chatglm3_6b_32k"
        ]:
            self.apply_residual_connection_post_layernorm = args.apply_residual_connection_post_layernorm
            self.norm = RmsNorm if args.rmsnorm else LayerNorm
            attention_mask_type = AttentionMaskType.causal
            position_embedding_type = PositionEmbeddingType.rope_gptj
            if args.model_name in ["chatglm2_6b_32k", "chatglm3_6b_32k"]:
                rotary_embedding_scaling = {
                    "type": "linear",
                    "factor": args.rotary_embedding_scaling
                }
        elif args.model_name in ["glm_10b"]:
            self.apply_residual_connection_post_layernorm = args.apply_residual_connection_post_layernorm
            self.norm = LayerNorm
            attention_mask_type = AttentionMaskType.bidirectionalglm
            position_embedding_type = PositionEmbeddingType.learned_absolute

        self.pre_norm = self.norm(
            normalized_shape=args.hidden_size,
            eps=args.norm_epsilon,
            elementwise_affine=True,
            dtype=args.dtype,
        )

        self.attention = Attention(
            hidden_size=args.hidden_size,
            num_attention_heads=args.num_heads,
            num_kv_heads=args.num_kv_heads,
            max_position_embeddings=args.max_seq_length,
            num_layers=args.num_layers,
            apply_query_key_layer_scaling=args.apply_query_key_layer_scaling,
            attention_mask_type=attention_mask_type,
            bias=args.qkv_bias,
            dtype=args.dtype,
            position_embedding_type=position_embedding_type,
            rotary_embedding_base=10000.0,
            rotary_embedding_scaling=rotary_embedding_scaling,
            use_int8_kv_cache=args.quant_mode.has_int8_kv_cache(),
            rotary_embedding_percentage=0.5,
            tp_group=args.mapping.tp_group,
            tp_size=args.mapping.tp_size,
            tp_rank=args.mapping.rank,
            quant_mode=args.quant_mode,
            q_scaling=1.0,
            cross_attention=False,
            relative_attention=False,
            max_distance=0,
            num_buckets=0,
            instance_id=layer_id * 2,
            dense_bias=args.linear_bias,
        )

        self.mlp = MLP(
            hidden_size=args.hidden_size,
            ffn_hidden_size=args.ffn_hidden_size,
            hidden_act=args.hidden_act,
            bias=args.linear_bias,
            dtype=args.dtype,
            tp_group=args.mapping.tp_group,
            tp_size=args.mapping.tp_size,
            quant_mode=args.quant_mode,
            instance_id=layer_id * 2 + 1,
        )

        self.post_norm = self.norm(
            normalized_shape=args.hidden_size,
            eps=args.norm_epsilon,
            elementwise_affine=True,
            dtype=args.dtype,
        )

    def forward(
        self,
        hidden_states: Tensor,
        position_ids: Tensor = None,  # only used in ChatGLM-6B
        kv_cache_params: KeyValueCacheParams = None,
        attention_params: AttentionParams = None,
    ):

        norm_output = self.pre_norm(hidden_states)

        attention_output = self.attention(
            hidden_states=norm_output,
            attention_mask=None,
            use_cache=self.use_cache,
            kv_cache_params=kv_cache_params,
            attention_params=attention_params,
            encoder_output=None,
            workspace=None,
            position_embedding=position_ids,
        )

        if self.use_cache:
            attention_output, presents = attention_output

        if self.model_name in ["chatglm_6b"]:
            residual = norm_output

            norm_input = residual * self.alpha + attention_output

            norm_output = self.post_norm(norm_input)

            mlp_output = self.mlp(norm_output)

            residual = norm_output

            output = residual * self.alpha + mlp_output

        elif self.model_name in [
                "chatglm2_6b", "chatglm2_6b_32k", "chatglm3_6b",
                "chatglm3_6b_base", "chatglm3_6b_32k", "glm_10b"
        ]:
            residual = norm_output if self.apply_residual_connection_post_layernorm else hidden_states

            norm_input = residual + attention_output

            norm_output = self.post_norm(norm_input)

            mlp_output = self.mlp(norm_output)

            residual = norm_output if self.apply_residual_connection_post_layernorm else norm_input

            output = residual + mlp_output

        return (output, presents) if self.use_cache else output


class ChatGLMModel(Module):

    def __init__(self, args):

        super().__init__()

        self.model_name = args.model_name

        if args.model_name in ["chatglm_6b", "glm_10b"]:
            self.norm = LayerNorm
        elif args.model_name in [
                "chatglm2_6b", "chatglm2_6b_32k", "chatglm3_6b",
                "chatglm3_6b_base", "chatglm3_6b_32k"
        ]:
            self.norm = RmsNorm
        self.use_cache = args.use_cache

        self.embedding = Embedding(
            num_embeddings=args.vocab_size,
            embedding_dim=args.hidden_size,
            dtype=args.dtype,
            tp_size=1,  #args.mapping.tp_size,
            tp_group=None,  #args.mapping.tp_group,
            sharding_dim=0,
            tp_rank=0,  #args.mapping.rank,
            instance_id=args.num_layers * 2,
        )

        if args.model_name in ["glm_10b"]:
            self.position_embeddings = Embedding(
                args.max_seq_length + 1,
                args.hidden_size,
                dtype=args.dtype,
                tp_size=1,  #args.mapping.tp_size,
                tp_group=None,  #args.mapping.tp_group,
                sharding_dim=0,
                tp_rank=0,  #args.mapping.rank,
                instance_id=args.num_layers * 2,
            )
            self.block_embeddings = Embedding(
                args.max_seq_length + 1,
                args.hidden_size,
                dtype=args.dtype,
                tp_size=1,  #args.mapping.tp_size,
                tp_group=None,  #args.mapping.tp_group,
                sharding_dim=0,
                tp_rank=0,  #args.mapping.rank,
                instance_id=args.num_layers * 2,
            )

        self.layers = ModuleList(
            ChatGLMDecoderLayer(i, args) for i in range(args.num_layers))

        self.final_norm = self.norm(
            normalized_shape=args.hidden_size,
            eps=args.norm_epsilon,
            elementwise_affine=True,
            dtype=args.dtype,
        )

    def forward(
        self,
        input_ids: Tensor = None,
        position_ids: Tensor = None,  # only used in ChatGLM-6B
        kv_cache_params: KeyValueCacheParams = None,
        attention_params: AttentionParams = None,
    ):

        hidden_states = self.embedding(input_ids)

        if self.model_name in ["glm_10b"]:
            position_ids_list = position_ids.split(1, dim=1)
            position_embedding = self.position_embeddings(position_ids_list[0])
            block_embedding = self.block_embeddings(position_ids_list[1])
            position_embedding = position_embedding + block_embedding

            position_embedding = position_embedding.view(
                concat([
                    shape(input_ids, 0),
                    shape(input_ids, 1),
                    4096,
                ]))

            hidden_states = hidden_states + position_embedding

        kv_cache_params.fill_none_tensor_list(len(self.layers))

        if self.use_cache:
            presents = []

        for layer, past, pointer, max_kv_cache_length in zip(
                self.layers, kv_cache_params.past_key_value,
                kv_cache_params.kv_cache_block_pointers,
                kv_cache_params.host_max_kv_cache_lengths):
            layer_output = layer(
                hidden_states,
                position_ids,
                kv_cache_params=KeyValueCacheParams(
                    past_key_value=[past],
                    kv_cache_block_pointers=[pointer],
                    host_past_key_value_lengths=kv_cache_params.
                    host_past_key_value_lengths,
                    host_max_kv_cache_lengths=max_kv_cache_length,
                    cache_indirection=kv_cache_params.cache_indirection,
                ),
                attention_params=attention_params,
            )

            if self.use_cache:
                hidden_states = layer_output[0]
                presents.append(layer_output[1])

        hidden_states = self.final_norm(hidden_states)

        return (hidden_states,
                tuple(presents)) if self.use_cache else hidden_states


class ChatGLMHeadModel(ChatGLMModel, GenerationMixin):

    def __init__(self, **args):

        if "args" not in args.keys():
            new_args = argparse.Namespace()
            for key, value in args.items():
                new_args.__setattr__(key, value)
            assert "model_name" in args.keys(), "model_name not set"
            # Other default values
            new_args.norm_epsilon = 1.0e-5
            new_args.tokens_per_block = 64
            new_args.use_cache = True
            if new_args.model_name in ["chatglm_6b"]:
                new_args.ffn_hidden_size = 16384
                new_args.linear_bias = True
                new_args.max_seq_length = min(2048,
                                              new_args.max_position_embeddings)
                new_args.num_kv_heads = 32
                new_args.qkv_bias = True
            elif new_args.model_name in ["glm_10b"]:
                new_args.ffn_hidden_size = 16384
                new_args.linear_bias = True
                new_args.max_seq_length = min(1024,
                                              new_args.max_position_embeddings)
                new_args.num_kv_heads = 32
                new_args.qkv_bias = True
            elif new_args.model_name in [
                    "chatglm2_6b", "chatglm2_6b_32k", "chatglm3_6b",
                    "chatglm3_6b_base", "chatglm3_6b_32k"
            ]:
                new_args.apply_residual_connection_post_layernorm = False
                new_args.ffn_hidden_size = 13696
                new_args.linear_bias = False
                new_args.num_kv_heads = 2
                new_args.qkv_bias = True
                new_args.rmsnorm = True
            args = new_args
        else:
            args = args["args"]

        self.init(args)

    def init(self, args):

        super().__init__(args)

        if isinstance(args.dtype, str):
            self.kv_dtype = str_dtype_to_trt(args.dtype)
        else:
            assert isinstance(args.dtype, trt.DataType)
            self.kv_dtype = args.dtype
        self.dtype = self.kv_dtype

        if isinstance(args.logits_dtype, str):
            self.logits_dtype = str_dtype_to_trt(args.logits_dtype)
        else:
            assert isinstance(args.logits_dtype, trt.DataType)
            self.logits_dtype = args.logits_dtype

        if args.quant_mode.has_int8_kv_cache():
            self.kv_dtype = str_dtype_to_trt('int8')
        elif args.quant_mode.has_fp8_kv_cache():
            self.kv_dtype = str_dtype_to_trt('fp8')

        self.hidden_size = args.hidden_size
        self.mapping = args.mapping
        self.max_num_tokens = args.max_output_len + args.max_input_len
        self.model_name = args.model_name
        self.num_heads = args.num_heads
        self.num_kv_heads = args.num_kv_heads
        self.num_layers = args.num_layers
        self.tokens_per_block = args.tokens_per_block
        self.use_cache = args.use_cache

        self.lm_head = ColumnLinear(
            in_features=self.hidden_size,
            out_features=pad_vocab_size(args.vocab_size, self.mapping.tp_size),
            bias=False,
            dtype=self.dtype,
            tp_group=self.mapping.tp_group,
            tp_size=self.mapping.tp_size,
            gather_output=True,
        )

    def forward(
        self,
        input_ids: Tensor = None,
        position_ids: Tensor = None,  # only used in ChatGLM-6B
        last_token_ids: Tensor = None,
        kv_cache_params: KeyValueCacheParams = None,
        attention_params: AttentionParams = None,
    ):

        hidden_states = super().forward(
            input_ids,
            position_ids,
            kv_cache_params,
            attention_params,
        )

        if self.use_cache:
            hidden_states, presents = hidden_states

        hidden_states = gather_last_token_logits(
            hidden_states, last_token_ids,
            default_net().plugin_config.remove_input_padding)

        lm_logits = self.lm_head(hidden_states)
        lm_logits.mark_output('logits', self.logits_dtype)

        if self.use_cache and default_net(
        ).plugin_config.paged_kv_cache == False:
            for i, present in enumerate(presents):
                present.mark_output(f'present_key_value_{i}', self.kv_dtype)
            return (lm_logits, presents)

        return lm_logits

    def prepare_inputs(
        self,
        max_batch_size: int = 0,
        max_input_len: int = 0,
        max_new_tokens: int = 0,
        use_cache: bool = True,
        max_beam_width: int = 1,
    ):
        '''@brief: Prepare inputs Tensors for the model, the given sizes are used to determine the
            ranges of the dimensions of when using TRT dynamic shapes.

            @return: a list contains values which can be fed into the self.forward()
        '''

        model_inputs = self.prepare_basic_inputs(
            max_batch_size=max_batch_size,
            max_beam_width=max_beam_width,
            max_input_len=max_input_len,
            max_new_tokens=max_new_tokens,
            num_kv_heads=self.num_kv_heads // self.mapping.tp_size,
            head_size=self.hidden_size // self.num_heads,
            num_layers=self.num_layers,
            kv_dtype=self.kv_dtype,
            remove_input_padding=default_net(
            ).plugin_config.remove_input_padding,
            use_gpt_attention_plugin=default_net().plugin_config.
            gpt_attention_plugin,
            use_gemm_plugin=default_net().plugin_config.gemm_plugin,
            use_custom_all_reduce=False,
            paged_kv_cache=default_net().plugin_config.paged_kv_cache,
            tokens_per_block=self.tokens_per_block,
            gather_all_token_logits=False,
            dtype=self.kv_dtype,
            num_heads=self.num_heads,
            mapping=self.mapping,
            max_num_tokens=self.max_num_tokens,
            prompt_embedding_table_size=0,
            position_encoding_2d=(self.model_name in ["chatglm_6b", "glm_10b"]),
        )

        return (model_inputs['input_ids'], model_inputs['position_ids'],
                model_inputs['last_token_ids'],
                KeyValueCacheParams(
                    past_key_value=model_inputs['past_key_value'],
                    host_past_key_value_lengths=model_inputs[
                        'host_past_key_value_lengths'],
                    host_max_kv_cache_lengths=model_inputs[
                        'host_max_kv_cache_lengths'],
                    kv_cache_block_pointers=model_inputs[
                        'kv_cache_block_pointers_list'],
                    cache_indirection=model_inputs['cache_indirection'],
                ),
                AttentionParams(
                    sequence_length=model_inputs['sequence_length'],
                    context_lengths=model_inputs['context_lengths'],
                    host_context_lengths=model_inputs['host_context_lengths'],
                    max_context_length=max_input_len,
                    host_request_types=model_inputs['host_request_types'],
                ))
