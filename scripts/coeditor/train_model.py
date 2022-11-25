import os
import random
from typing import *

import wandb
from coeditor.common import *
from coeditor.dataset import TokenizedEditDataset
from coeditor.encoding import WindowArgs
from coeditor.model import *
from prepare_data import make_or_load_datasets


def check_save_dir(model_name: str):
    to_check = [get_model_dir(b) / model_name for b in [True, False]]
    exists = [path for path in to_check if path.exists()]
    if exists:
        for path in exists:
            print(f"Path already exists:", path)
        answer = input("Continue training? (y/n):")
        if answer.lower().strip() != "y":
            print("Training aborted.")
            exit(1)


os.chdir(proj_root())

dataset_name = "medium"
model_variant = "-analysis-post_usees"

data_args = DataTransformArgs(
    shuffle_extra_ids=True,
)
train_args = TrainingArgs(
    max_batch_tokens=4096,
    window=WindowArgs(4096),
    quicktest=True,
)
valid_args = EvalArgs(
    max_batch_tokens=4096 * 2,
    window=WindowArgs(4096),
)
test_args = EvalArgs(
    max_batch_tokens=4096 * 2,
    window=WindowArgs(4096),
)
dec_args = DecodingArgs()

model_name = f"coeditor-{dataset_name}"
model_name += model_variant
if train_args.quicktest:
    model_name = "quicktest-" + model_name

check_save_dir(model_name)

datasets = make_or_load_datasets("medium")

config_dict = {
    k: get_modified_args(v)
    for k, v in {
        "data_args": data_args,
        "train_args": train_args,
        "valid_args": valid_args,
        "test_args": test_args,
        "dec_args": dec_args,
    }.items()
}

project = "Coeditor" if not train_args.quicktest else "Coeditor-quicktest"
wandb.init(dir="..", project=project, name=model_name, config=config_dict)

if train_args.quicktest:
    print("Using fewer data for quick test.")
    for name, dataset in datasets.items():
        datasets[name] = TokenizedEditDataset.from_edits(list(dataset.all_edits())[:10])

model = CoeditorModel.from_code_t5(data_args)

if os.getenv("CUDA_VISIBLE_DEVICES") is None:
    warnings.warn(
        "CUDA_VISIBLE_DEVICES not set, using 0. Note that "
        "the Huggingface Trainer will use all visible GPUs for training."
    )
    os.environ["CUDA_VISIBLE_DEVICES"] = "0"


with timed_action("Training"):
    model.train_on_data(
        model_name, datasets["train"], datasets["valid"], train_args, valid_args
    )

with timed_action("Loss Evaluating"):
    eval_result = model.eval_loss_on_data(datasets["test"], test_args)
    eval_dict = {f"test/{k}": v.average() for k, v in eval_result.items()}
    wandb.log(eval_dict)

with timed_action("Accuracy Evaluating"):
    dec_result = model.predict_on_data(datasets["test"], test_args, dec_args)
    pickle_dump(get_model_dir() / model_name / "dec_result.pkl", dec_result)
    wandb.log({"test/exact-acc": dec_result.exact_match_accuracy().average()})

with timed_action("Saving samples"):
    max_saved_samples = 200
    random.seed(42)
    exs_to_save = list(range(len(dec_result.predictions)))
    random.shuffle(exs_to_save)
    exs_to_save = exs_to_save[:max_saved_samples]
    out_dir = get_model_dir() / model_name / "pred_samples"
    dec_result.save_examples_to_dir(out_dir, exs_to_save)
    print("Output examples saved to:", out_dir)
