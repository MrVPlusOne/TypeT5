import copy
from .common import *
from transformers.models.t5.modeling_t5 import (
    T5Config,
    T5Attention,
    T5LayerNorm,
    T5Block,
    T5Stack,
    T5LayerSelfAttention,
    T5LayerCrossAttention,
    T5LayerFF,
    T5ForConditionalGeneration,
    BaseModelOutputWithPastAndCrossAttentions,
    Seq2SeqLMOutput,
    T5PreTrainedModel,
)
import torch
from torch import BoolTensor, FloatTensor, LongTensor, Tensor
from torch import nn
from coeditor.encoding import BOS_id, EOS_id, encode_basic
import transformers

PAD_id = 0


class RetrievalEditorModel(T5PreTrainedModel):
    """
    A CodeT5 model that takes in multiple reference code snippets and a
    query code snippet with multiple masked spans and perdicts the maksed spans.

    While the computational cost of a normal CodeT5 encoder increases quadratically,
    this model only increases linearly with the number of reference code snippets.
    """

    def __init__(self, config: T5Config):
        super().__init__(config)
        self.model_dim = config.d_model

        self.shared = nn.Embedding(config.vocab_size, config.d_model)

        encoder_config = copy.deepcopy(config)
        encoder_config.is_decoder = False
        encoder_config.use_cache = False
        encoder_config.is_encoder_decoder = False
        self.encoder = T5Stack(encoder_config, self.shared)

        decoder_config = copy.deepcopy(config)
        decoder_config.is_decoder = True
        decoder_config.is_encoder_decoder = False
        decoder_config.num_layers = config.num_decoder_layers
        self.decoder = T5Stack(decoder_config, self.shared)

        self.lm_head = nn.Linear(config.d_model, config.vocab_size, bias=False)

        # Initialize weights and apply final processing
        self.post_init()

        self.query_attened_ref = True

        # Model parallel
        # self.model_parallel = False
        # self.device_map = None

    def encode_token_seqs(
        self, references: list[TokenSeq] | list[str], pad_id=None
    ) -> LongTensor:
        references = [encode_basic(ref) for ref in references if isinstance(ref, str)]
        max_len = max(len(ref) for ref in references)
        if pad_id is None:
            pad_id = PAD_id
        rows = []
        for ref in references:
            row = ref + [pad_id] * (max_len - len(ref))
            rows.append(row)
        out = LongTensor(rows).to(self.device)
        return cast(LongTensor, out)

    def forward(
        self,
        # encoder args
        input_ids: LongTensor | None = None,  # queries
        references: LongTensor | None = None,
        ref_masks: list[list[int]] | None = None,
        labels: LongTensor | None = None,
        # decoder args
        encoder_outputs: "RetrivalEncoderOutputs | None" = None,
        decoder_input_ids: LongTensor | None = None,
        decoder_inputs_embeds: Tensor | None = None,
        decoder_attention_mask: Tensor | None = None,
        past_key_values=None,
        use_cache=None,
        # not used args below
        output_attentions=None,
        output_hidden_states=None,
        return_dict=None,
    ) -> Seq2SeqLMOutput:
        """
        Shapes
        - input_ids: (n_queries, seq_len,)
        - references: (num_refs, ref_len)
        - ref_masks: for each query, a list of reference indices. If none,
        assume all references are accessible to all queries.
        """
        if labels is not None:
            assert_eq(labels.dim(), 2)

        if encoder_outputs is None:
            assert input_ids is not None
            encoder = self.get_encoder()
            encoder_outputs = encoder.forward(input_ids, references, ref_masks)

        if labels is not None and decoder_input_ids is None:
            # get decoder inputs from shifting lm labels to the right
            decoder_input_ids = cast(LongTensor, self._shift_right(labels))

        decoder_outputs = self.decoder.forward(
            input_ids=decoder_input_ids,
            inputs_embeds=decoder_inputs_embeds,
            attention_mask=decoder_attention_mask,
            encoder_hidden_states=encoder_outputs.last_hidden_state,
            encoder_attention_mask=encoder_outputs.hidden_state_mask,
            past_key_values=past_key_values,
            use_cache=use_cache,
            return_dict=True,
        )
        assert isinstance(decoder_outputs, BaseModelOutputWithPastAndCrossAttentions)

        sequence_output = decoder_outputs[0]
        if self.config.tie_word_embeddings:
            # Rescale output before projecting on vocab
            # See https://github.com/tensorflow/mesh/blob/fa19d69eafc9a482aff0b59ddd96b025c0cb207d/mesh_tensorflow/transformer/transformer.py#L586
            sequence_output = sequence_output * (self.model_dim**-0.5)

        lm_logits = self.lm_head(sequence_output)

        loss = None
        if labels is not None:
            loss_fct = nn.CrossEntropyLoss(ignore_index=-100)
            loss = loss_fct(lm_logits.view(-1, lm_logits.size(-1)), labels.view(-1))

        return Seq2SeqLMOutput(
            loss=loss,
            logits=lm_logits,
            past_key_values=decoder_outputs.past_key_values,
            decoder_hidden_states=decoder_outputs.hidden_states,
            decoder_attentions=decoder_outputs.attentions,
            cross_attentions=decoder_outputs.cross_attentions,
            encoder_last_hidden_state=cast(Any, encoder_outputs.last_hidden_state),
            # encoder_hidden_states=encoder_outputs.hidden_states,
            # encoder_attentions=encoder_outputs.attentions,
        )

    def prepare_inputs_for_generation(
        self,
        input_ids,
        references,
        past=None,
        attention_mask=None,
        head_mask=None,
        decoder_head_mask=None,
        cross_attn_head_mask=None,
        use_cache=None,
        encoder_outputs=None,
        **kwargs,
    ):

        # cut decoder_input_ids if past is used
        if past is not None:
            input_ids = input_ids[:, -1:]

        return {
            "decoder_input_ids": input_ids,
            "references": references,
            "past_key_values": past,
            "encoder_outputs": encoder_outputs,
            # "attention_mask": attention_mask,
            # "head_mask": head_mask,
            # "decoder_head_mask": decoder_head_mask,
            # "cross_attn_head_mask": cross_attn_head_mask,
            "use_cache": use_cache,
        }

    def prepare_decoder_input_ids_from_labels(self, labels: torch.Tensor):
        return self._shift_right(labels)

    def get_input_embeddings(self):
        return self.shared

    def set_input_embeddings(self, new_embeddings):
        self.shared = new_embeddings
        self.encoder.set_input_embeddings(new_embeddings)
        self.decoder.set_input_embeddings(new_embeddings)

    def set_output_embeddings(self, new_embeddings):
        self.lm_head = new_embeddings

    def get_output_embeddings(self):
        return self.lm_head

    def get_encoder(self):
        return RetrivalEncoder(self.encoder, query_attened_ref=self.query_attened_ref)

    def get_decoder(self):
        return self.decoder

    def _reorder_cache(self, past, beam_idx):
        return T5ForConditionalGeneration._reorder_cache(
            cast(Any, None), past, beam_idx
        )

    @staticmethod
    def from_code_t5(size: Literal["small", "base", "large"]):
        model = RetrievalEditorModel.from_pretrained(f"Salesforce/codet5-{size}")
        assert isinstance(model, RetrievalEditorModel)
        return model


@dataclass
class RetrivalEncoderOutputs(transformers.utils.ModelOutput):
    last_hidden_state: Tensor
    hidden_state_mask: Tensor | None = None


@dataclass
class RetrivalEncoder:
    encoder: T5Stack
    query_attened_ref: bool

    def __call__(self, *args: Any, **kwds: Any) -> RetrivalEncoderOutputs:
        return self.forward(*args, **kwds)

    def forward(
        self,
        input_ids: LongTensor,
        references: LongTensor | None = None,
        ref_query_list: list[list[int]] | None = None,
        # not used arguments below:
        output_attentions=None,
        output_hidden_states=None,
        return_dict=None,
    ) -> RetrivalEncoderOutputs:
        """
        Shapes
        - input_ids: (n_queries, seq_len)
        - references: (num_refs, ref_len)
        - ref_masks: for each query, a list of reference indices. If none,
        assume all references are accessible to all queries.
        """
        if references is None:
            references = cast(LongTensor, LongTensor([[PAD_id]]).to(input_ids.device))

        assert_eq(input_ids.dim(), 2)
        assert_eq(references.dim(), 2)

        n_queries = input_ids.size(0)
        query_attention_mask = cast(BoolTensor, input_ids.ne(PAD_id))

        n_refs = references.size(0)
        ref_attention_mask = cast(BoolTensor, references.ne(PAD_id))

        if ref_query_list is None:
            ref_query_list = [list(range(n_refs)) for _ in range(n_queries)]

        ref_outputs = self.encode_references(
            references,
            attention_mask=ref_attention_mask,
            output_hidden_states=self.query_attened_ref,
        )

        if self.query_attened_ref:
            last_hidden_state, hidden_state_mask = self.encode_query_complex(
                query_ids=input_ids,
                query_attention_mask=query_attention_mask,
                ref_outputs=ref_outputs,
                ref_query_list=ref_query_list,
                ref_attention_mask=ref_attention_mask,
            )
        else:
            last_hidden_state, hidden_state_mask = self.encode_query_simple(
                query_ids=input_ids,
                query_attention_mask=query_attention_mask,
                ref_outputs=ref_outputs,
                ref_query_list=ref_query_list,
                ref_attention_mask=ref_attention_mask,
            )

        return RetrivalEncoderOutputs(
            last_hidden_state=last_hidden_state, hidden_state_mask=hidden_state_mask
        )

    def encode_references(
        self,
        input_ids: LongTensor,
        attention_mask: Tensor | None = None,
        output_hidden_states: bool | None = None,
    ) -> BaseModelOutputWithPastAndCrossAttentions:
        """input_ids: shape (num_refs, seq_len)"""
        out = self.encoder.forward(
            input_ids,
            attention_mask=attention_mask,
            output_hidden_states=output_hidden_states,
            return_dict=True,
        )
        assert isinstance(out, BaseModelOutputWithPastAndCrossAttentions)
        return out

    def encode_query_simple(
        self,
        query_ids: LongTensor,
        query_attention_mask: Tensor,
        ref_outputs: BaseModelOutputWithPastAndCrossAttentions,
        ref_query_list: list[list[int]],
        ref_attention_mask: Tensor,
    ) -> tuple[Tensor, Tensor]:
        n_queries = len(ref_query_list)
        ref_states = ref_outputs.last_hidden_state
        n_refs, ref_len, model_d = ref_states.shape

        ref_state_list = [ref_states[i, ref_attention_mask[i]] for i in range(n_refs)]

        query_outputs = self.encode_references(
            query_ids, attention_mask=query_attention_mask
        )
        query_states = query_outputs.last_hidden_state
        query_state_list = [
            query_states[i, query_attention_mask[i]] for i in range(n_queries)
        ]

        qref_rows = []
        for q in range(n_queries):
            qrefs = [ref_state_list[r] for r in ref_query_list[q]]
            qrefs.append(query_state_list[q])
            row_tensor = torch.cat(qrefs, dim=0)
            assert row_tensor.ndim == 2  # (sum(ref_lens) + query_len, model_dim)
            qref_rows.append(row_tensor)
        qref_states, qref_masks = stack_pad_tensors(qref_rows)
        assert_eq(qref_states.size(0), n_queries)
        if (qref_len := qref_states.size(1)) > (
            pad_len := ref_len * n_refs + query_ids.size(1)
        ):
            raise AssertionError(f"{qref_len = }, {pad_len = }")

        assert_eq(qref_states.size(2), model_d)
        return qref_states, qref_masks

    def encode_query_complex(
        self,
        query_ids: LongTensor,
        query_attention_mask: BoolTensor,
        ref_outputs: BaseModelOutputWithPastAndCrossAttentions,
        ref_query_list: list[list[int]],
        ref_attention_mask: BoolTensor,
    ) -> tuple[Tensor, Tensor]:
        assert (
            query_ids[:, 0].ne(PAD_id).all()
        ), "queries must be padded only at the end."
        n_queries = len(ref_query_list)

        qref_hidden_states = list[Tensor]()
        qref_attention_masks = list[BoolTensor]()
        for ref_states in not_none(ref_outputs.hidden_states):
            n_refs, ref_len, model_d = ref_states.shape
            ref_state_list = [
                ref_states[i, ref_attention_mask[i]] for i in range(n_refs)
            ]

            qref_rows = []
            for q in range(n_queries):
                qrefs = [ref_state_list[r] for r in ref_query_list[q]]
                qref_rows.append(torch.cat(qrefs, dim=0))  # (sum(ref_lens), model_dim)
            # qrefs are padded at the end
            qref_states, qref_masks = stack_pad_tensors(
                qref_rows
            )  # (n_queries, sum(ref_lens), model_dim)
            qref_hidden_states.append(qref_states)
            qref_attention_masks.append(qref_masks)

        query_outputs = encode_query_stack(
            stack=self.encoder,
            input_ids=query_ids,
            ref_hidden_states=tuple(qref_hidden_states),
            input_attention_mask=query_attention_mask,
            ref_attention_mask=qref_attention_masks[0],
        )

        # concat last hidden states
        query_states = query_outputs.last_hidden_state
        ref_states = qref_hidden_states[-1]
        ref_mask = qref_attention_masks[-1]

        combine_rows = []
        for q in range(n_queries):
            query_s = query_states[q, query_attention_mask[q]]
            ref_s = ref_states[q, ref_mask[q]]
            combine_rows.append(torch.cat([ref_s, query_s], dim=0))
        return stack_pad_tensors(combine_rows)


def stack_pad_tensors(xs: Sequence[Tensor]) -> tuple[Tensor, BoolTensor]:
    """Pad a list of tensors at the end. Return the padded tensor and a mask."""
    padded = nn.utils.rnn.pad_sequence(list(xs), batch_first=True)
    n_batch, n_len = padded.shape[:2]
    mask = cast(BoolTensor, padded.new_zeros(n_batch, n_len, dtype=torch.bool))
    for i, x in enumerate(xs):
        mask[i, : x.shape[0]] = True
    return padded, mask


def t5_cross_attention(
    layer: T5LayerSelfAttention,
    hidden_states,
    key_value_states,
    position_bias=None,
    output_attentions=False,
) -> tuple[Tensor, ...]:
    """Use a self attention layer as a cross attention layer.
    Note that you should encode any attention mask directly into position_bias.
    """
    normed_hidden_states = layer.layer_norm(hidden_states)
    normed_key_value_states = layer.layer_norm(key_value_states)
    attention_output = layer.SelfAttention.forward(
        normed_hidden_states,
        key_value_states=normed_key_value_states,
        position_bias=position_bias,
        output_attentions=output_attentions,
        # layer_head_mask=layer_head_mask,
        # past_key_value=past_key_value,
        # use_cache=use_cache,
        # query_length=query_length,
    )
    hidden_states = hidden_states + layer.dropout(attention_output[0])
    outputs = (hidden_states,) + attention_output[1:]
    return cast(tuple[Tensor, ...], outputs)


def encode_query_block(
    block: T5Block,
    query_hidden_states: Tensor,  # (n_queries, query_len, model_dim)
    ref_hidden_states: Tensor,  # (n_queries, ref_len, model_dim)
    position_bias: Tensor,
    output_attentions: bool = False,
) -> tuple[Tensor, ...]:
    """Run a T5Block to encode the query. Instead of using self-attention, this uses
    a hybrid attention where the query is allowed to attend to both itself and the references.
    """

    layer0 = block.layer[0]
    assert isinstance(layer0, T5LayerSelfAttention)
    key_value_states = torch.cat([ref_hidden_states, query_hidden_states], dim=1)
    hybrid_attention_outputs = t5_cross_attention(
        layer0,
        query_hidden_states,
        key_value_states=key_value_states,
        position_bias=position_bias,
        output_attentions=output_attentions,
    )
    hidden_states = hybrid_attention_outputs[0]

    # clamp inf values to enable fp16 training
    if hidden_states.dtype == torch.float16 and torch.isinf(hidden_states).any():
        clamp_value = torch.finfo(hidden_states.dtype).max - 1000
        hidden_states = torch.clamp(hidden_states, min=-clamp_value, max=clamp_value)

    # Apply Feed Forward layer
    ff_layer = block.layer[-1]
    assert isinstance(ff_layer, T5LayerFF)
    hidden_states: Tensor = ff_layer.forward(hidden_states)

    # clamp inf values to enable fp16 training
    if hidden_states.dtype == torch.float16 and torch.isinf(hidden_states).any():
        clamp_value = torch.finfo(hidden_states.dtype).max - 1000
        hidden_states = torch.clamp(hidden_states, min=-clamp_value, max=clamp_value)

    return (hidden_states, *hybrid_attention_outputs[1:])


def encode_query_stack(
    stack: T5Stack,
    input_ids: LongTensor,  # (n_queries, query_len)
    ref_hidden_states: tuple[Tensor, ...],  # tuples of (n_queries, ref_len, model_dim)
    input_attention_mask: BoolTensor | None = None,  # (n_queries, query_len)
    ref_attention_mask: BoolTensor | None = None,  # (n_queries, ref_len)
) -> BaseModelOutputWithPastAndCrossAttentions:
    """Run a T5Stack to encode the query. Instead of using self-attention, this uses
    a hybrid attention where the query is allowed to attend to both itself and the references.
    """
    assert not stack.is_decoder
    device = input_ids.device

    assert input_ids[:, 0].ne(PAD_id).all(), "input_ids must be padded at only the end."

    input_shape = input_ids.size()
    batch_size, query_len = input_shape
    _, ref_len, model_dim = ref_hidden_states[0].size()

    if input_attention_mask is None:
        input_attention_mask = cast(
            BoolTensor, torch.ones(batch_size, query_len, dtype=torch.bool).to(device)
        )
    if ref_attention_mask is None:
        ref_attention_mask = cast(
            BoolTensor, torch.ones(batch_size, ref_len, dtype=torch.bool).to(device)
        )

    # combine input and ref attention masks
    attention_mask = input_attention_mask.unsqueeze(2) * torch.cat(
        [ref_attention_mask, input_attention_mask], dim=1
    ).unsqueeze(1)

    assert_eq(tuple(attention_mask.shape), (batch_size, query_len, query_len + ref_len))

    extended_attention_mask = stack.get_extended_attention_mask(
        attention_mask, input_shape
    )

    attention_layer = cast(T5Block, stack.block[0]).layer[0].SelfAttention
    assert isinstance(attention_layer, T5Attention)

    n_queries = input_ids.size(0)
    ref_lens = ref_attention_mask.sum(dim=1)[:, None]  # (n_queries, 1)
    # relative pos needs to be of shape (n_quries, query_len, ref_len + query_len)
    ref_pos = torch.arange(ref_len, device=device, dtype=torch.long)[
        None, :
    ]  # (1, ref_len)
    ref_pos = ref_pos + torch.zeros(
        n_queries, 1, device=device, dtype=torch.long
    )  # (n_queries, ref_len)
    query_pos = (
        torch.arange(query_len, device=device, dtype=torch.long)[None, :] + ref_lens
    )  # (n_queries, query_len)
    key_pos = torch.cat([ref_pos, query_pos], dim=1)  # (n_queries, ref_len + query_len)
    relative_pos = (
        key_pos[:, None, :] - query_pos[:, :, None]
    )  # (n_queries, query_len, ref_len + query_len)
    position_bias = compute_bias(attention_layer, relative_pos)
    position_bias = extended_attention_mask + position_bias

    assert stack.embed_tokens is not None
    inputs_embeds = stack.embed_tokens(input_ids)
    hidden_states = stack.dropout(inputs_embeds)

    for i, block in enumerate(stack.block):
        # Model parallel
        assert isinstance(block, T5Block)
        ref_states = ref_hidden_states[i]
        layer_outputs = encode_query_block(
            block,
            hidden_states,
            ref_states,
            position_bias=position_bias,
            output_attentions=False,
        )

        # layer_outputs is a tuple with:
        # hidden-states, key-value-states, (self-attention position bias), (self-attention weights), (cross-attention position bias), (cross-attention weights)
        layer_outputs = layer_outputs[:1] + (None,) + layer_outputs[1:]

        hidden_states, present_key_value_state = layer_outputs[:2]
        assert isinstance(hidden_states, Tensor)

    hidden_states = stack.final_layer_norm(hidden_states)
    hidden_states = stack.dropout(hidden_states)

    return BaseModelOutputWithPastAndCrossAttentions(
        last_hidden_state=hidden_states,
        # past_key_values=present_key_value_states,
        # hidden_states=all_hidden_states,
        # attentions=all_attentions,
        # cross_attentions=all_cross_attentions,
    )


def compute_bias(
    self: T5Attention,
    relative_pos: Tensor,
) -> Tensor:
    """Compute binned relative position bias from `relative_pos` of
    the shape `(n_queries, query_length, key_length)`"""
    relative_position_bucket = self._relative_position_bucket(
        relative_pos,  # shape (query_length, key_len)
        bidirectional=(not self.is_decoder),
        num_buckets=self.relative_attention_num_buckets,
        max_distance=self.relative_attention_max_distance,
    )
    values = self.relative_attention_bias(
        relative_position_bucket
    )  # shape (n_qureis, query_len, key_len, n_heads)
    values = values.permute(
        [0, 3, 1, 2]
    )  # shape (n_queries, n_heads, query_len, key_len)
    return values
