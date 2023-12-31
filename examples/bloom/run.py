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
import json
import os

import torch
from transformers import BloomTokenizerFast

import tensorrt_llm
from tensorrt_llm.runtime import ModelConfig, SamplingConfig

from build import get_engine_name  # isort:skip

EOS_TOKEN = 2
PAD_TOKEN = 3


def parse_arguments():
    parser = argparse.ArgumentParser()
    parser.add_argument('--max_output_len', type=int, required=True)
    parser.add_argument('--max_kv_cache_len',
                        type=int,
                        default=None,
                        help='The max kv cache length. \
              If the final sequence length exceeds the kv cache length, we will enable cyclic kv cache. \
              If it is set to None, we will use the max sequence length.')
    parser.add_argument('--log_level', type=str, default='error')
    parser.add_argument('--engine_dir', type=str, default='bloom_outputs')
    parser.add_argument('--tokenizer_dir',
                        type=str,
                        default=".",
                        help="Directory containing the tokenizer.model.")
    parser.add_argument('--input_text',
                        type=str,
                        default='Born in north-east France, Soyer trained as a')
    return parser.parse_args()


if __name__ == '__main__':
    args = parse_arguments()
    tensorrt_llm.logger.set_level(args.log_level)

    config_path = os.path.join(args.engine_dir, 'config.json')
    with open(config_path, 'r') as f:
        config = json.load(f)
    use_gpt_attention_plugin = config['plugin_config']['gpt_attention_plugin']
    dtype = config['builder_config']['precision']
    world_size = config['builder_config']['tensor_parallel']
    assert world_size == tensorrt_llm.mpi_world_size(), \
        f'Engine world size ({world_size}) != Runtime world size ({tensorrt_llm.mpi_world_size()})'
    num_heads = config['builder_config']['num_heads'] // world_size
    hidden_size = config['builder_config']['hidden_size'] // world_size
    vocab_size = config['builder_config']['vocab_size']
    num_layers = config['builder_config']['num_layers']

    runtime_rank = tensorrt_llm.mpi_rank()
    runtime_mapping = tensorrt_llm.Mapping(world_size,
                                           runtime_rank,
                                           tp_size=world_size)
    torch.cuda.set_device(runtime_rank % runtime_mapping.gpus_per_node)

    engine_name = get_engine_name('bloom', dtype, world_size, runtime_rank)
    serialize_path = os.path.join(args.engine_dir, engine_name)

    tokenizer = BloomTokenizerFast.from_pretrained(args.tokenizer_dir)
    input_ids = torch.tensor(tokenizer.encode(args.input_text),
                             dtype=torch.int32).cuda().unsqueeze(0)

    model_config = ModelConfig(num_heads=num_heads,
                               num_kv_heads=num_heads,
                               hidden_size=hidden_size,
                               vocab_size=vocab_size,
                               num_layers=num_layers,
                               gpt_attention_plugin=use_gpt_attention_plugin,
                               dtype=dtype)
    sampling_config = SamplingConfig(end_id=EOS_TOKEN, pad_id=PAD_TOKEN)
    input_lengths = torch.tensor(
        [input_ids.size(1) for _ in range(input_ids.size(0))]).int().cuda()

    with open(serialize_path, 'rb') as f:
        engine_buffer = f.read()
    decoder = tensorrt_llm.runtime.GenerationSession(model_config,
                                                     engine_buffer,
                                                     runtime_mapping)
    decoder.setup(input_ids.size(0),
                  max_context_length=input_ids.size(1),
                  max_new_tokens=args.max_output_len,
                  max_kv_cache_length=args.max_kv_cache_len)
    output_ids = decoder.decode(input_ids, input_lengths, sampling_config)
    torch.cuda.synchronize()

    output_ids = output_ids.tolist()[0][0][input_ids.size(1):]
    output_text = tokenizer.decode(output_ids)
    print(f'Input: {args.input_text}')
    print(f'Output Ids: {output_ids}')
    print(f'Output: {output_text}')
