from dataclasses import field
import math
import torch

import wandb
from coeditor.dataset import TokenizedEditDataset
from coeditor.encoding import (
    TokenizedEdit,
    WindowArgs,
    _Tokenizer,
    is_extra_id,
    BOS_id,
    EOS_id,
)
from spot.data import output_ids_as_seqs
from spot.model import dynamic_dataloader, DataLoader
from .common import *
from transformers.models.t5.modeling_t5 import (
    T5ForConditionalGeneration,
    Seq2SeqLMOutput,
)
from transformers import (
    Seq2SeqTrainingArguments,
    Seq2SeqTrainer,
    DataCollatorForSeq2Seq,
    EarlyStoppingCallback,
)
from datasets.arrow_dataset import Dataset

CodeT5Model = T5ForConditionalGeneration


@dataclass
class DecodingArgs:
    base_tokens: int = 128
    tokens_per_line: int = 16
    do_sample: bool = False
    top_p: float = 0.9
    num_beams: Optional[int] = 8
    length_penalty: float = 1.0


@dataclass
class TrainingArgs:
    max_batch_tokens: int
    window: WindowArgs
    learning_rate: float = 2e-5
    weight_decay: float = 0.01
    quicktest: bool = False


@dataclass
class EvalArgs:
    max_batch_tokens: int
    window: WindowArgs


@dataclass
class CoeditorModel:
    """
    args:
    - skip_unchanged: whether to skip <extra_id>s that are not followed by a
    change in the output sequence.
    """

    codet5: CodeT5Model
    skip_unchanged: bool = False

    @property
    def coeditor_args(self):
        return {"skip_unchanged": self.skip_unchanged}

    @torch.autocast("cuda")
    def predict(
        self, input_tks: TokenSeq, decode_args: DecodingArgs | None = None
    ) -> TokenSeq:
        if decode_args is None:
            decode_args = DecodingArgs()
        x = torch.tensor([input_tks]).to(self.codet5.device)
        n_lines = sum(is_extra_id(tk) for tk in input_tks)
        max_length = decode_args.base_tokens + decode_args.tokens_per_line * n_lines
        output = self.codet5.generate(
            x,
            max_length,
            do_sample=decode_args.do_sample,
            top_p=decode_args.top_p,
            num_beams=decode_args.num_beams,
            length_penalty=decode_args.length_penalty,
        )[0]
        return output.tolist()

    def save_pretrained(self, path: Path):
        pickle_dump(path / "coeditor_args.pkl", self.coeditor_args)
        self.codet5.save_pretrained(path)

    def to(self, device):
        self.codet5.to(device)

    def train_on_data(
        self,
        training_name: str,
        train_data: TokenizedEditDataset,
        eval_data: TokenizedEditDataset,
        train_args: "TrainingArgs",
        eval_args: "EvalArgs",
    ) -> None:
        train_coeditor_model(
            self, training_name, train_data, eval_data, train_args, eval_args
        )

    def eval_on_data(
        self,
        eval_data: TokenizedEditDataset,
        eval_args: "EvalArgs",
    ):
        eval_edits = list(eval_data.all_edits())
        eval_loader = edits_to_dataloader(
            [e.truncate_ctx(eval_args.window) for e in eval_edits],
            eval_args.max_batch_tokens,
            skip_unchanged=self.skip_unchanged,
            shuffle=True,
        )
        return eval_label_likelihood(self, eval_loader)

    @staticmethod
    def load_pretrained(path: Path):
        codet5 = CodeT5Model.from_pretrained(path)
        assert isinstance(codet5, CodeT5Model)
        args: dict = pickle_load(path / "coeditor_args.pkl")
        skip_unchanged = args.get("skip_unchanged", False)
        return CoeditorModel(codet5, skip_unchanged=skip_unchanged)

    @staticmethod
    def from_code_t5(use_small_model=False):
        path = (
            "Salesforce/codet5-small" if use_small_model else "Salesforce/codet5-base"
        )
        codet5 = CodeT5Model.from_pretrained(path)
        assert isinstance(codet5, CodeT5Model)
        codet5.resize_token_embeddings(len(_Tokenizer))
        return CoeditorModel(codet5)


def train_coeditor_model(
    model: CoeditorModel,
    training_name: str,
    train_data: TokenizedEditDataset,
    eval_data: TokenizedEditDataset,
    train_args: TrainingArgs,
    eval_args: EvalArgs,
):
    train_dir = get_model_dir(trained=False) / training_name

    train_edits = list(train_data.all_edits())
    eval_edits = list(eval_data.all_edits())
    if train_args.quicktest:
        train_edits = train_edits[:10]
        eval_edits = eval_edits[:10]

    train_lodader = edits_to_dataloader(
        [e.truncate_ctx(train_args.window) for e in train_edits],
        train_args.max_batch_tokens,
        skip_unchanged=model.skip_unchanged,
        shuffle=True,
    )
    eval_loader = edits_to_dataloader(
        [e.truncate_ctx(eval_args.window) for e in eval_edits],
        eval_args.max_batch_tokens,
        skip_unchanged=model.skip_unchanged,
        shuffle=True,
    )

    class DynamicTrainer(Seq2SeqTrainer):
        def get_train_dataloader(self):
            return train_lodader

        def get_eval_dataloader(self, eval_dataset):
            return eval_loader

    trainer_args = Seq2SeqTrainingArguments(
        output_dir=str(train_dir),
        overwrite_output_dir=True,
        evaluation_strategy="epoch",
        save_strategy="epoch",
        logging_steps=400,
        prediction_loss_only=True,
        learning_rate=train_args.learning_rate,
        weight_decay=train_args.weight_decay,
        num_train_epochs=3 if train_args.quicktest else 5,
        fp16=True,
        load_best_model_at_end=True,
        push_to_hub=False,
        report_to=["wandb"],
    )

    trainer = DynamicTrainer(
        model.codet5, trainer_args, callbacks=[EarlyStoppingCallback()]
    )

    trainer.train()
    save_dir = get_model_dir(trained=True) / training_name
    model.save_pretrained(save_dir)
    print("Model saved to:", save_dir)


@torch.no_grad()
@torch.autocast("cuda")
def eval_label_likelihood(
    model: CoeditorModel,
    eval_loader: DataLoader,
):
    """
    Compute the perplexity of the model on the eval dataset.
    Note that special tokens like <extra_id> and <s> are excluded
    from the perplexity computation.
    """
    core = model.codet5
    core.eval()
    loss_per_ex = WeightedSum(0.0, 0)
    loss_per_tk = WeightedSum(0.0, 0)
    prob_per_ex = WeightedSum(0.0, 0)
    for batch in tqdm(eval_loader, desc="evaluate loss", unit="batch"):
        input_ids = batch["input_ids"].to(core.device)
        labels = batch["labels"].to(core.device)
        attention_mask = batch["attention_mask"].to(core.device)
        outputs = core.forward(input_ids, labels=labels, attention_mask=attention_mask)
        assert isinstance(outputs, Seq2SeqLMOutput)
        logits = outputs.logits.permute(0, 2, 1)  # shape: (batch, vocab, seq_len)
        logp = torch.nn.functional.cross_entropy(
            logits,
            labels,
            reduction="none",
            ignore_index=-100,
        )  # shape: (batch, seq_len)
        loss = logp.sum().item()
        ex_prob = torch.exp(-logp.sum(dim=1)).sum().item()
        bsize = input_ids.shape[0]
        label_tks = (labels != -100).sum().item()
        loss_per_ex += WeightedSum(loss, bsize)
        loss_per_tk += WeightedSum(loss, label_tks)
        prob_per_ex += WeightedSum(ex_prob, bsize)

    return {
        "loss_per_ex": loss_per_ex,
        "loss_per_tk": loss_per_tk,
        "prob_per_ex": prob_per_ex,
    }


def code_tk_loss(logits: torch.FloatTensor, labels: torch.LongTensor):
    # This computation depends on the label sequence length, which may not be
    # suitable for comparing different encoding schemes.
    special_tks = {
        _Tokenizer.pad_token_id,
        _Tokenizer.eos_token_id,
        _Tokenizer.bos_token_id,
        -100,
    }

    def is_code_token(tk):
        return not is_extra_id(tk) and tk not in special_tks

    cpu_labels = cast(torch.LongTensor, labels.cpu())
    rows = logits.size(0)
    n_tokens = 0
    total_loss = 0.0
    for i in range(rows):
        selected = [is_code_token(tk) for tk in cpu_labels[i].tolist()]
        ce = torch.nn.functional.cross_entropy(
            logits[i, selected], labels[i, selected], reduction="sum"
        )
        n_tokens += sum(selected)
        total_loss += float(ce.item())

    return total_loss, n_tokens


def wrap_bos(x: TokenSeq) -> TokenSeq:
    if x:
        assert x[0] != BOS_id
    return [BOS_id] + x + [EOS_id]


def drop_empty_labels(x: TokenSeq) -> TokenSeq:
    """Drop the <extra_id>s that are not followed by a code token."""
    new_seq = TokenSeq()
    for k, v in output_ids_as_seqs(x).items():
        if v:
            new_seq.append(k)
            new_seq.extend(v)
    return new_seq


def edits_to_dataset(edits: Sequence[TokenizedEdit], skip_unchanged: bool) -> Dataset:
    def get_labels(e: TokenizedEdit):
        labels = e.output_tks
        if skip_unchanged:
            labels = drop_empty_labels(labels)
        return wrap_bos(labels)

    return Dataset.from_dict(
        {
            "input_ids": [e.input_tks for e in edits],
            "labels": [get_labels(e) for e in edits],
        }
    )


def edits_to_dataloader(
    edits: Sequence[TokenizedEdit],
    max_batch_tokens: int,
    skip_unchanged: bool,
    shuffle: bool = False,
) -> DataLoader:
    dataset = edits_to_dataset(edits, skip_unchanged)
    data_collator = DataCollatorForSeq2Seq(_Tokenizer)
    return dynamic_dataloader(dataset, max_batch_tokens, data_collator, shuffle=shuffle)
