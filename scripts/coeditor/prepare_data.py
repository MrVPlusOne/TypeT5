from coeditor.common import *
from coeditor.ctx_change_encoder import C3ProblemGenerator
from coeditor.dataset import *
from spot.utils import run_long_task

if __name__ == "__main__":
    os.chdir(proj_root())

    dataset_name = "xl"
    encoder = C3ProblemGenerator()
    with run_long_task(f"Preparing dataset {dataset_name} with encoder {encoder}"):
        problems = make_or_load_datasets(dataset_name, encoder, recreate_data=True)

    tokenizer = C3ProblemTokenizer()
    for name, probs in problems.items():
        print("=" * 40, name, "=" * 40)
        stats = tokenizer._compute_stats(probs)
        pretty_print_dict(stats)
