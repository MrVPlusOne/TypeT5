from coeditor.encoders import has_change
from coeditor.encoding import (
    Del_id,
    Newline_id,
    TokenizedEdit,
    TruncateAt,
    break_into_chunks,
    change_to_line_diffs,
    change_to_tokens,
    encode_basic,
    get_extra_id,
    line_diffs_to_original_delta,
    truncate_output_tks,
    truncate_section,
    truncate_sections,
)
from coeditor.history import Added, Change, Modified
from spot.static_analysis import (
    ModuleName,
    ProjectPath,
    sort_modules_by_imports,
    ModuleHierarchy,
)
from .common import *
import jedi, parso
from parso.python import tree
from jedi.api import helpers, convert_names, classes
from .code_change import (
    ChangeScope,
    ChangedSpan,
    JModule,
    JProjectChange,
    LineRange,
    ProjectChangeProcessor,
    ProjectState,
    JModuleChange,
    StatementSpan,
    line_range,
)
from parso.python import tree as ptree
from cachetools import FIFOCache
import jedi.cache
import jedi.parser_utils
import parso.cache


@dataclass
class C3Problem:
    "Contextual code change prediction problem."
    span: ChangedSpan
    # most relevant to least relevant
    relevant_changes: list[ChangedSpan]
    # most relevant to least relevant
    relevant_unchanged: list[ChangedSpan]
    # some optional information about how the problem was generated
    src_info: dict[str, Any]


PyFullName = NewType("PyFullName", str)


@dataclass(frozen=True)
class PyDefinition:
    """Note that the module and positions can be referring to either the import
    statement or the actual definition."""

    full_name: PyFullName
    start_pos: tuple[int, int]
    end_pos: tuple[int, int]

    @staticmethod
    def from_name(name: classes.BaseName) -> Iterable["PyDefinition"]:
        if (
            not name.in_builtin_module()
            and (full_name := name.full_name)
            # and (import_module := name.module_name)
            and (start_pos := name.get_definition_start_position())
            and (end_pos := name.get_definition_end_position())
        ):
            full_name = PyFullName(full_name)
            # if not full_name.startswith(import_module):
            #     raise ValueError(f"Inconsistent module: {full_name=}, {import_module=}")
            yield PyDefinition(full_name, start_pos, end_pos)

    @staticmethod
    def from_scope(scope: ChangeScope) -> "PyDefinition":
        path = scope.path
        full_name = PyFullName(f"{path.module}.{path.path}")
        start_pos = scope.header_line_range[0], 0
        end_pos = scope.header_line_range[1], 0
        return PyDefinition(full_name, start_pos, end_pos)


@dataclass(frozen=True)
class LineUsageAnalysis:
    line2usages: Mapping[int, set[PyDefinition]]


class C3ProblemGenerator(ProjectChangeProcessor[C3Problem]):
    VERSION = "1.2"

    def __init__(self, analyzer: "JediUsageAnalyzer | None" = None):
        if analyzer is None:
            analyzer = JediUsageAnalyzer()

        self.analyzer = analyzer
        # whether to only generate problems for editing functions
        self._is_training: bool = False

    def __repr__(self) -> str:
        return repr_modified_args(self)

    def append_stats(self, stats: dict[str, Any]) -> None:
        rec_add_dict_to(stats, {"analyzer_errors": self.analyzer.error_counts})

    def clear_stats(self) -> None:
        return self.analyzer.error_counts.clear()

    def set_training(self, is_training: bool) -> None:
        self._is_training = is_training

    def pre_edit_analysis(
        self,
        pstate: ProjectState,
        modules: Mapping[RelPath, JModule],
        changes: Mapping[ModuleName, JModuleChange],
    ) -> Mapping[ModuleName, LineUsageAnalysis]:
        "Return the definition usages of each line."
        project = pstate.project
        result = dict[ModuleName, LineUsageAnalysis]()

        src_map = {m.mname: f for f, m in modules.items()}
        for mname, mchange in changes.items():
            if not isinstance(mchange.module_change, Modified):
                continue

            lines_to_analyze = set[int]()
            for span in mchange.changed.values():
                if span.change is Added:
                    continue
                lines_to_analyze.update(range(*span.line_range))
                lines_to_analyze.update(range(*span.header_line_range))

            mod_path = src_map[mname]
            script = pstate.scripts[mod_path]
            line_usages = self.analyzer.get_line_usages(
                script, project.path, lines_to_analyze, silent=True
            )
            result[mname] = line_usages
        return result

    def post_edit_analysis(
        self,
        pstate: ProjectState,
        modules: Mapping[RelPath, JModule],
        changes: Mapping[ModuleName, JModuleChange],
    ) -> list[ModuleName]:
        "Return the topological order among the modules."
        # sort modules topologically
        module_deps = dict[ModuleName, set[ModuleName]]()
        for rel_path, module in modules.items():
            names = {n for n in module.imported_names}
            script = pstate.scripts[rel_path]
            deps = module_deps.setdefault(module.mname, set())
            for n in names:
                try:
                    srcs = _fast_goto(
                        script, n, follow_imports=True, follow_builtin_imports=False
                    )
                except Exception as e:
                    self.analyzer.add_error(str(e))
                    continue
                for source in srcs:
                    deps.add(source.module_name)
        module_order = sort_modules_by_imports(module_deps)
        return module_order

    def process_change(
        self,
        pchange: JProjectChange,
        mod2usages: Mapping[ModuleName, LineUsageAnalysis],
        module_order: Sequence[ModuleName],
    ) -> Sequence[C3Problem]:
        before_mod_map = {m.mname: m for m in pchange.all_modules.before}
        mod_hier = ModuleHierarchy.from_modules(before_mod_map)
        cspan_cache = dict[PyDefinition, list[ChangedSpan]]()

        def get_def_spans(used: PyDefinition) -> list[ChangedSpan]:
            "Get the (pre-edit) spans for the given definition."
            if used.full_name in cspan_cache:
                return cspan_cache[used.full_name]
            path = mod_hier.resolve_path(split_dots(used.full_name))
            cspans = list[ChangedSpan]()
            if path is None or (jmod := before_mod_map.get(path.module)) is None:
                cspan_cache[used] = cspans
                return cspans
            scope = jmod.as_scope
            elem = scope._search(path.path, used.start_pos[0])
            func_scopes = list[ChangeScope]()
            stmt_spans = list[StatementSpan]()
            match elem:
                case ChangeScope(tree=ptree.Function()):
                    func_scopes.append(elem)
                case ChangeScope(tree=ptree.Class()):
                    # add all attrs and methods
                    stmt_spans.extend(elem.spans)
                    func_scopes.extend(
                        s
                        for s in elem.subscopes.values()
                        if isinstance(s.tree, ptree.Function)
                    )
                case StatementSpan():
                    stmt_spans.append(elem)

            # add collapsed functions
            for f_scope in func_scopes:
                ancestors = f_scope.ancestors()
                stmts = f_scope.spans[-1].statements
                body_code = stmts[-1].get_code().strip("\n")
                if len(stmts) > 1:
                    ellipsis = "    " * (len(ancestors) - 1) + "# ...\n"
                    body_code = ellipsis + body_code
                cspan = ChangedSpan(
                    Modified.from_unchanged(body_code),
                    [Modified.from_unchanged(s) for s in ancestors],
                    f_scope.spans[-1].line_range,
                )
                cspans.append(cspan)

            # add statement spans
            for stmt_span in stmt_spans:
                ancestors = stmt_span.scope.ancestors()
                body_code = stmt_span.code
                cspan = ChangedSpan(
                    Modified.from_unchanged(body_code),
                    [Modified.from_unchanged(s) for s in ancestors],
                    stmt_span.line_range,
                )
                cspans.append(cspan)

            cspan_cache[used] = cspans
            return cspans

        def get_relevant_unchanged(
            this_change: ChangedSpan, other_changes: Sequence[ChangedSpan]
        ):
            if isinstance(this_change.change, Added):
                # nothing to analyze
                return []
            path = this_change.path
            line_usages = mod2usages[path.module]
            # parent defs are also considered as used
            parent_defs = [
                PyDefinition.from_scope(c.earlier()) for c in this_change.parent_scopes
            ]
            # immediate parents are more relevant
            sorted_defs = list(reversed(parent_defs))
            used_defs = set(sorted_defs)
            all_lines = set(range(*this_change.line_range))
            all_lines.update(range(*this_change.header_line_range))
            for l in all_lines:
                for pydef in line_usages.line2usages.get(l, set()):
                    if (
                        pydef.full_name.startswith(path.module)
                        and pydef.start_pos[0] in all_lines
                    ):
                        # skip self references
                        continue
                    if pydef not in used_defs:
                        used_defs.add(pydef)
                        sorted_defs.append(pydef)

            # return unique cspans
            seen = set[tuple[ModuleName, LineRange]]()
            # we don't need to show the changed parts again
            for cspan in (this_change, *other_changes):
                seen.add((cspan.path.module, cspan.line_range))
            result = list[ChangedSpan]()
            for used in sorted_defs:
                for cspan in get_def_spans(used):
                    key = (cspan.path.module, cspan.line_range)
                    if key not in seen:
                        result.append(cspan)
                        seen.add(key)
            return result

        processed_cspans = list[ChangedSpan]()
        problems = list[C3Problem]()
        for m in module_order:
            if (mchange := pchange.changed.get(m)) is None:
                continue
            for span in mchange.changed.values():
                should_mk_problem = (span.change.as_char() == Modified.as_char()) and (
                    # only consider function edits at test time
                    self._is_training
                    or span._is_func_body()
                )
                if should_mk_problem:
                    # latest changes are more relevant
                    relevant_changes = list(reversed(processed_cspans))
                    prob = C3Problem(
                        span,
                        relevant_changes=relevant_changes,
                        relevant_unchanged=get_relevant_unchanged(
                            span, relevant_changes
                        ),
                        src_info={"commit": pchange.commit_info},
                    )
                    problems.append(prob)
                processed_cspans.append(span)
        return problems


@dataclass(frozen=True)
class TkC3Problem(TokenizedEdit):
    "Tokenized contextual code change prediction problem."
    input_tks: TokenSeq
    output_tks: TokenSeq
    path: ProjectPath
    change_type: Change[None]
    # most relevant to least relevant
    named_references: Sequence[tuple[str, TokenSeq]]
    src_info: dict[str, Any]

    @property
    def references(self) -> Sequence[TokenSeq]:
        return [ref for name, ref in self.named_references]

    def __repr__(self):
        return f"TkC3Problem(path={self.path}, type={self.change_type.as_char()}, stats={self.stats()})"

    @property
    def main_tks(self):
        return self.input_tks

    def show(self) -> str:
        return self.show_prediction(None)

    def all_ctxs(self) -> dict[str, TokenSeq]:
        return {name: ref for name, ref in self.named_references}

    def meta_data_lines(self) -> list[str]:
        return [
            f"path: {self.path}",
            f"n_references: {len(self.references)}",
            f"total_reference_tks: {sum(len(ref) for ref in self.references)}",
            f"src_info: {self.src_info}",
        ]

    def stats(self) -> Mapping[str, int | float]:
        return {
            "input_tks": len(self.input_tks),
            "output_tks": len(self.output_tks),
            "n_references": len(self.references),
            "total_reference_tks": sum(len(ref) for ref in self.references),
        }


_ObjId = NewType("_ObjId", int)


@dataclass
class C3ProblemTokenizer:
    VERSION = "1.0"
    max_ref_tks: int = 512
    max_query_tks: int = 512
    max_output_tks: int = 256
    max_scope_tks: int = 128
    max_lines_to_edit: int = 20
    ref_chunk_overlap: int = 32
    max_total_ref_tks: int = 512 * 64  # a very large threshold
    max_chunks_per_elem: int = 4
    skip_unchanged_problems: bool = True

    def __post_init__(self):
        self._id_cache = FIFOCache[_ObjId, TokenSeq](maxsize=1000)
        self._scope_cache = FIFOCache[_ObjId, TokenSeq](maxsize=1000)
        self._value_cache = FIFOCache[Any, Sequence[TokenSeq]](maxsize=1000)

    def tokenize_problem(
        self,
        problem: C3Problem,
    ) -> Sequence[TkC3Problem]:
        span = problem.span
        named_references = list[tuple[str, TokenSeq]]()
        # compute the references that are relevant to this span
        relevant_chunks = self._group_encode_changed_refs(problem.relevant_changes)
        for i, chunk in enumerate(relevant_chunks):
            named_references.append((f"changed ref {i}", chunk))
        relevant_chunks = self._group_encode_unchanged_refs(problem.relevant_unchanged)
        for i, chunk in enumerate(relevant_chunks):
            named_references.append((f"unchanged ref {i}", chunk))

        diffs = change_to_line_diffs(span.change)
        original, delta = line_diffs_to_original_delta(diffs)
        origin_lines = split_list(encode_basic(original), Newline_id)
        tk_delta = delta.to_tk_delta()
        chunk_id = 0
        chunk_start_l = 0
        scope_tks = self._encode_parent_scopes(span.parent_scopes, 0)
        chunk_input = TokenSeq()
        input_limit = self.max_query_tks - len(scope_tks)
        chunk_lines = 0
        chunk_output = TokenSeq()
        prev_change_tks = TokenSeq()

        def get_problem(chunk_input, chunk_output):
            # try move some prev_change_tks into the input
            above_tks = prev_change_tks
            below_tks = join_list(origin_lines[l:], Newline_id)
            chunk_input, above_tks, below_tks = self._inline_some_context(
                chunk_input, above_tks, below_tks, input_limit
            )

            # limit the input size if it's too long (can happen for later chunks)
            chunk_input = truncate_section(chunk_input, TruncateAt.Right, input_limit)
            chunk_output = truncate_output_tks(chunk_input, chunk_output)
            chunk_output = truncate_section(
                chunk_output, TruncateAt.Right, self.max_output_tks, add_bos=False
            )

            above_chunks = break_into_chunks(
                above_tks,
                lambda i: self._encode_parent_scopes(span.parent_scopes, -1 - i),
                chunk_size=self.max_ref_tks,
                overlap=self.ref_chunk_overlap,
                right_to_left=True,
            )
            if finished:
                below_chunks = []
            else:
                below_chunks = break_into_chunks(
                    below_tks,
                    lambda i: self._encode_parent_scopes(span.parent_scopes, i + 1),
                    chunk_size=self.max_ref_tks,
                    overlap=self.ref_chunk_overlap,
                )
            above_chunks = [
                (f"above chunk {i}", chunk) for i, chunk in enumerate(above_chunks)
            ]
            below_chunks = [
                (f"below chunk {i}", chunk) for i, chunk in enumerate(below_chunks)
            ]
            all_refs = above_chunks + below_chunks + named_references
            size_sum = 0
            kept_refs = list[tuple[str, TokenSeq]]()
            for (name, chunk) in all_refs:
                if size_sum + len(chunk) <= self.max_total_ref_tks:
                    size_sum += len(chunk)
                    kept_refs.append((name, chunk))

            return TkC3Problem(
                scope_tks + chunk_input,
                chunk_output,
                path=span.parent_scopes[-1].earlier().path,
                change_type=span.change.map(lambda _: None),
                named_references=kept_refs,
                src_info=problem.src_info,
            )

        problems = list[TkC3Problem]()
        for l in range(len(tk_delta.deltas) + 1):
            finished = l == len(tk_delta.deltas)
            input_growth = len(origin_lines[l]) + 2 if l < len(origin_lines) else 1
            if (
                finished
                or chunk_lines >= self.max_lines_to_edit
                or len(chunk_input) + input_growth > input_limit
            ):
                if has_change(chunk_output):
                    problems.append(get_problem(chunk_input, chunk_output))
                    if len(problems) >= self.max_chunks_per_elem:
                        break

                if finished:
                    break

                chunk_main_input = join_list(origin_lines[chunk_start_l:l], Newline_id)
                chunk_main_delta = tk_delta.for_input_range((chunk_start_l, l))
                chunk_main_change = chunk_main_delta.to_change_tks(chunk_main_input)
                prev_change_tks.extend(chunk_main_change)
                prev_change_tks.append(Newline_id)
                chunk_id += 1
                chunk_input = TokenSeq()
                chunk_lines = 0
                chunk_output = TokenSeq()
                chunk_start_l = l

            chunk_input.append(get_extra_id(chunk_lines))
            if l < len(origin_lines):
                chunk_input.extend(origin_lines[l])
                chunk_input.append(Newline_id)
            line_change = join_list(tk_delta.deltas[l], Newline_id)
            chunk_output.append(get_extra_id(chunk_lines))
            chunk_output.extend(line_change)
            if line_change and line_change[-1] != Del_id:
                chunk_output.append(Newline_id)
            chunk_lines += 1
        return problems

    def _encode_scope_change(self, c: Change[ChangeScope]) -> TokenSeq:
        if (key := _ObjId(id(c))) in self._scope_cache:
            return self._scope_cache[key]
        hchange = c.map(lambda s: s.header_code.strip("\n"))
        tks = truncate_section(
            change_to_tokens(hchange), TruncateAt.Left, self.max_scope_tks
        )
        self._scope_cache[key] = tks
        return tks

    def _encode_parent_scopes(
        self, scope_changes: Sequence[Change[ChangeScope]], offset: int
    ) -> TokenSeq:
        scope_tks = join_list(
            (self._encode_scope_change(c) for c in scope_changes), Newline_id
        )
        if offset != 0:
            scope_tks.extend(encode_basic(f"\n# offset: {offset}\n"))
        else:
            scope_tks.append(Newline_id)
        scope_tks = truncate_section(scope_tks, TruncateAt.Left, self.max_scope_tks)
        return scope_tks

    def _inline_some_context(
        self,
        input: TokenSeq,
        above_ctx: TokenSeq,
        below_ctx: TokenSeq,
        size_limit: int,
    ) -> tuple[TokenSeq, TokenSeq, TokenSeq]:
        "try move some some of the ctx tokens into the input if there's space."
        extra_space = size_limit - len(input)
        if (above_ctx or below_ctx) and extra_space > 0:
            truncated_above, truncated_below = truncate_sections(
                extra_space,
                (above_ctx, TruncateAt.Left),
                (below_ctx, TruncateAt.Right),
                add_bos=True,
            )
            above_left = len(above_ctx) - len(truncated_above)
            if above_left > 0:
                above_ctx = truncate_section(
                    above_ctx, TruncateAt.Right, above_left + self.ref_chunk_overlap
                )
            else:
                above_ctx = TokenSeq()
            below_left = len(below_ctx) - len(truncated_below)
            if below_left > 0:
                below_ctx = truncate_section(
                    below_ctx, TruncateAt.Left, below_left + self.ref_chunk_overlap
                )
            else:
                below_ctx = TokenSeq()
            input = truncated_above + input + truncated_below
        return input, above_ctx, below_ctx

    def _encode_change(self, change: Change[str]) -> TokenSeq:
        if (key := _ObjId(id(change))) in self._id_cache:
            return self._id_cache[key]
        change = change.map(lambda s: s.strip("\n"))
        change_tks = change_to_tokens(change)
        self._id_cache[key] = change_tks
        return change_tks

    def _group_encode_unchanged_refs(
        self, elems: Sequence[ChangedSpan]
    ) -> Sequence[TokenSeq]:
        return self._group_encode_changed_refs(elems)

    def _group_encode_changed_refs(
        self, changes: Sequence[ChangedSpan]
    ) -> Sequence[TokenSeq]:
        module2changes = groupby(changes, lambda c: c.path.module)
        all_chunks = list[TokenSeq]()
        for change_group in module2changes.values():
            change_group.sort(key=lambda c: c.line_range[0])
            file_tks = TokenSeq()
            # we'll add module as the chunk header, so we start within the module
            last_scope = change_group[0].parent_scopes[:1]
            for c in change_group:
                scope_diff = []
                for i, s in enumerate(c.parent_scopes):
                    if (
                        i >= len(last_scope)
                        or s.earlier().path != last_scope[i].earlier().path
                    ):
                        scope_diff.append(s)
                if scope_diff:
                    header_tks = self._encode_parent_scopes(scope_diff, 0)
                    file_tks.extend(header_tks)
                body_tks = self._encode_change(c.change)
                file_tks.extend(body_tks)
                file_tks.append(Newline_id)
                file_tks.append(Newline_id)
                last_scope = c.parent_scopes

            mod_change = change_group[0].parent_scopes[:1]
            mod_chunks = break_into_chunks(
                file_tks,
                lambda i: self._encode_parent_scopes(mod_change, i),
                self.max_ref_tks,
                overlap=self.ref_chunk_overlap,
            )
            all_chunks.extend(mod_chunks)
        return all_chunks

    def __repr__(self):
        return repr_modified_args(self)


@dataclass
class JediUsageAnalyzer:
    _KnownJediErrors = {
        "not enough values to unpack (expected 2",
        "'Newline' object has no attribute 'children'",
        "trailer_op is actually ",
        "There's a scope that was not managed: <Module",
        "maximum recursion depth exceeded",
        "'NoneType' object has no attribute 'type'",
    }

    def __post_init__(self):
        self.error_counts = dict[str, int]()
        self.tlogger: TimeLogger = TimeLogger()

    def get_line_usages(
        self,
        script: jedi.Script,
        proj_root: Path,
        lines_to_analyze: Collection[int],
        silent: bool = False,
    ):
        jmod: tree.Module = script._module_node
        line2usages = dict[int, set[PyDefinition]]()
        all_names = [
            name for k, names in jmod.get_used_names()._dict.items() for name in names
        ]
        all_names.sort(key=lambda x: x.start_pos)
        for name in tqdm(all_names, f"Analyzing {script.path}", disable=silent):
            name: tree.Name
            line = name.start_pos[0]
            if line not in lines_to_analyze:
                continue
            usages = line2usages.setdefault(line, set())
            try:
                defs = _fast_goto(
                    script,
                    name,
                    follow_imports=True,
                    follow_builtin_imports=False,
                )
                for d in defs:
                    usages.update(PyDefinition.from_name(d))

            except Exception as e:
                err_text = repr(e)
                str_limit = 40
                if len(err_text) > str_limit:
                    err_text = err_text[:str_limit] + "..."
                self.add_error(err_text)
        return LineUsageAnalysis(line2usages)

    def add_error(self, err_text: str):
        self.error_counts[err_text] = self.error_counts.get(err_text, 0) + 1

    @staticmethod
    def is_known_error(err_text: str):
        return any(k in err_text for k in JediUsageAnalyzer._KnownJediErrors)


def _fast_goto(
    script: jedi.Script,
    tree_name: tree.Name,
    *,
    follow_imports=False,
    follow_builtin_imports=False,
    only_stubs=False,
    prefer_stubs=False,
) -> set[classes.Name]:
    """
    Goes to the name that defined the object under the cursor. Optionally
    you can follow imports.
    Multiple objects may be returned, depending on an if you can have two
    different versions of a function.

    :param follow_imports: The method will follow imports.
    :param follow_builtin_imports: If ``follow_imports`` is True will try
        to look up names in builtins (i.e. compiled or extension modules).
    :param only_stubs: Only return stubs for this method.
    :param prefer_stubs: Prefer stubs to Python objects for this method.
    :rtype: list of :class:`.Name`
    """
    name = script._get_module_context().create_name(tree_name)

    # Make it possible to goto the super class function/attribute
    # definitions, when they are overwritten.
    names = []
    if name.tree_name.is_definition() and name.parent_context.is_class():
        class_node = name.parent_context.tree_node
        class_value = script._get_module_context().create_value(class_node)
        mro = class_value.py__mro__()
        next(mro)  # Ignore the first entry, because it's the class itself.
        for cls in mro:
            names = cls.goto(tree_name.value)
            if names:
                break

    if not names:
        names = list(name.goto())

    if follow_imports:
        names = helpers.filter_follow_imports(names, follow_builtin_imports)
    names = convert_names(
        names,
        only_stubs=only_stubs,
        prefer_stubs=prefer_stubs,
    )

    return {classes.Name(script._inference_state, d) for d in set(names)}


# fix jedi cache error
def _get_parso_cache_node(grammar, path):
    try:
        return jedi.parser_utils.parser_cache[grammar._hashed].get(path)
    except KeyError:
        return None


jedi.parser_utils.get_parso_cache_node = _get_parso_cache_node


def _fix_jedi_cache(cache_dir: Path):
    jedi.parser_utils.get_parso_cache_node = _get_parso_cache_node
    jedi.settings.cache_directory = cache_dir / "jedi_cache"
    parso.cache._default_cache_path = cache_dir / "parso_cache"
