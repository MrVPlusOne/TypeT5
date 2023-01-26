import shutil
import tempfile
import traceback

import coeditor.code_change
from coeditor.code_change import ProjectChangeProcessor, edits_from_commit_history
from coeditor.ctx_change_encoder import (
    C3Problem,
    C3ProblemGenerator,
    C3ProblemTokenizer,
    JediUsageAnalyzer,
    _fix_jedi_cache,
)
from coeditor.encoding import TEdit
from coeditor.history import Added, CommitInfo, get_commit_history
from spot.utils import pretty_print_dict, scalar_stats

from .common import *


@dataclass
class TokenizedEditDataset(Generic[TEdit]):
    _edits: list[TEdit]

    def __repr__(self) -> str:
        n_edits = len(self.all_edits())
        return f"TokenizedEditDataset(n_edits={n_edits})"

    def subset_edits(self, n_edits: int) -> "TokenizedEditDataset":
        return TokenizedEditDataset.from_edits(self.all_edits()[:n_edits])

    def overall_stats(self) -> dict:
        all_edits = self.all_edits()
        n_added = sum(isinstance(e.change_type, Added) for e in all_edits)
        basic_stats = {
            "n_edits": len(all_edits),
            "n_additions": n_added,
        }
        extra_stats = dict[str, list]()
        for e in all_edits:
            for k, v in e.stats().items():
                if k in extra_stats:
                    extra_stats[k].append(v)
                else:
                    extra_stats[k] = [v]
        return basic_stats | {k: scalar_stats(v) for k, v in extra_stats.items()}

    def all_edits(self) -> list[TEdit]:
        return self._edits

    @staticmethod
    def from_edits(edits: Iterable[TEdit]) -> "TokenizedEditDataset[TEdit]":
        return TokenizedEditDataset(list(edits))


@dataclass
class C3EditEncoder:
    change_processor: ProjectChangeProcessor[C3Problem] = field(
        default_factory=C3ProblemGenerator
    )
    edit_tokenizer: C3ProblemTokenizer = field(default_factory=C3ProblemTokenizer)


@dataclass
class _ProcessingResult:
    edits: Sequence[C3Problem]
    stats: dict[str, dict | Any]


time_limit_per_commit = 10.0


def _process_commits(
    root: Path,
    workdir: Path,
    commits: Sequence[CommitInfo],
    is_training: bool,
    change_processor: ProjectChangeProcessor[C3Problem],
) -> _ProcessingResult:
    # use process-specific parso cache
    _fix_jedi_cache(workdir)
    coeditor.code_change._tlogger.clear()
    change_processor.clear_stats()
    change_processor.set_training(is_training)
    try:
        # cannot return here since subprocess will be killed after returning
        edits = edits_from_commit_history(
            root,
            commits,
            tempdir=workdir / "code",
            change_processor=change_processor,
            silent=True,
            time_limit=time_limit_per_commit * (len(commits) + 10),
        )
    except Exception as e:
        if isinstance(e, KeyboardInterrupt):
            raise
        warnings.warn(f"Failed to process project: {root}\nError: {e}")
        traceback.print_exception(e, limit=-6)
        edits = []
    stats = dict()
    change_processor.append_stats(stats)
    rec_add_dict_to(stats, {"tlogger": coeditor.code_change._tlogger.times})
    return _ProcessingResult(edits, stats)


def dataset_from_projects(
    project_roots: Sequence[Path],
    change_processor: ProjectChangeProcessor[C3Problem],
    repo_training: Sequence[bool],
    max_history_per_repo: int = 1000,
    workers: int = DefaultWorkers,
) -> "Mapping[Path, Sequence[C3Problem]]":
    """
    Create a TokenizedEditDataset from a list of project roots and a given encoder.
    Args:
        - max_history_per_repo (int, optional): When the repo history is longer than
        this value, only the oldest portion is going to be used. Defaults to 1000.
    """
    workdir = Path(tempfile.gettempdir()) / "dataset_from_projects"
    histories = pmap(
        get_commit_history,
        project_roots,
        max_workers=workers,
        desc="Getting commit histories",
        tqdm_args={"unit": "repo"},
    )
    # keep the oldest portion of the history
    histories = [commits[-max_history_per_repo:] for commits in histories]
    # break long commit sequences into chunks for parallelization
    roots = list[Path]()
    chunk_training = list[bool]()
    chunked_histories = list[list[CommitInfo]]()
    for root, h, train in zip(project_roots, histories, repo_training):
        history_chunk_size = max(50, math.ceil(len(h) / 10))
        for i in range(0, len(h), history_chunk_size):
            roots.append(root)
            chunk_training.append(train)
            # note that we need 1 extra overlapping commit to get all diffs
            chunked_histories.append(h[i : i + history_chunk_size + 1])
    workdirs = [workdir / f"chunk-{i}" for i in range(len(roots))]
    try:
        presults = pmap(
            _process_commits,
            roots,
            workdirs,
            chunked_histories,
            chunk_training,
            key_args={"change_processor": change_processor},
            max_workers=workers,
            tqdm_args={"unit": "chunk"},
        )
    finally:
        if workdir.exists():
            shutil.rmtree(workdir)
            print("Workdir removed:", workdir)

    project2edits = dict[Path, list[C3Problem]]()

    try:
        stats = dict[str, Any]()
        for root, pr in zip(roots, presults):
            project2edits.setdefault(root, []).extend(pr.edits)
            rec_add_dict_to(stats, pr.stats)

        if "tlogger" in stats:
            df = TimeLogger.times_to_dataframe(stats.pop("tlogger"))
            print("Time stats:")
            display(df)
        if "analyzer_errors" in list(stats.keys()):
            errors: dict = stats.pop("analyzer_errors")
            for k in list(errors.keys()):
                if JediUsageAnalyzer.is_known_error(k):
                    errors.pop(k)
            if errors:
                print("Analyzer errors:")
                for k in sorted(errors.keys(), key=lambda k: errors[k], reverse=True):
                    print(f"{k}:\t{errors[k]}")
        if stats:
            print("Other Stats:")
            pretty_print_dict(stats)
    except Exception as e:
        if not isinstance(e, KeyboardInterrupt):
            print("Error while printing stats:", e)

    return project2edits


def datasets_from_repos(
    repos_root: Path,
    change_processor: ProjectChangeProcessor[C3Problem],
    max_history_per_repo: int = 1000,
    workers: int = DefaultWorkers,
) -> Mapping[str, Sequence[C3Problem]]:
    splits = ["test", "valid", "train"]
    projects = dict[str, list[Path]]()
    split_is_training = dict[str, list[bool]]()
    for split in splits:
        if not (repos_root / split).exists():
            warnings.warn(f"Split {split} not found at {repos_root / split}.")
            continue
        ps = [p for p in (repos_root / split).iterdir() if p.is_dir]
        projects[split] = ps
        training = split == "train"
        split_is_training[split] = [training] * len(ps)
        if not ps:
            warnings.warn(f"No projects found in {split} split")

    dataset = dataset_from_projects(
        join_list(projects.values()),
        change_processor=change_processor,
        repo_training=join_list(split_is_training.values()),
        max_history_per_repo=max_history_per_repo,
        workers=workers,
    )
    return {k: join_list(dataset[r] for r in repos) for k, repos in projects.items()}


def make_or_load_datasets(
    dataset_name: str,
    change_processor: ProjectChangeProcessor[C3Problem],
    recreate_data: bool = False,
    workers: int = DefaultWorkers,
) -> Mapping[str, Sequence[C3Problem]]:
    config_str = repr_modified_args(change_processor)

    save_dir = get_dataset_dir(dataset_name) / config_str

    if recreate_data or not save_dir.exists():
        if dataset_name == "SPOT":
            datasets = {
                "test": dataset_from_projects(
                    [proj_root()], change_processor, [False], workers=workers
                )
            }
        else:
            datasets = datasets_from_repos(
                get_dataset_dir(dataset_name) / "repos",
                change_processor,
                workers=workers,
            )
        with timed_action("Saving datasets to disk"):
            save_datasets(datasets, save_dir)
        print("Tokenized dataset saved to:", save_dir)
    else:
        with timed_action("Loading datasets from disk"):
            datasets = load_datasets(save_dir)

    size_info = run_command(["du", "-ha", "."], save_dir)
    print(f"Dataset sizes:")
    print(size_info)

    return datasets


def save_datasets(datasets: Mapping[str, Any], save_dir: Path) -> None:
    for name, dataset in datasets.items():
        pickle_dump(save_dir / f"{name}.pkl", dataset)
    subprocess.run(["du", "-sh", save_dir])


def load_datasets(
    save_dir: Path, splits=("test", "valid", "train")
) -> Mapping[str, Any]:
    return {
        name: pickle_load(path)
        for name in splits
        if (path := (save_dir / f"{name}.pkl")).exists()
    }


def get_repo_signature(repo: Path, n_commits: int = 30) -> tuple[str, ...]:
    # use the first n commits as the signature
    commits = get_commit_history(repo)[-n_commits:]
    return tuple(c.msg for c in commits)
