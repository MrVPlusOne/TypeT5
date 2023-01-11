from coeditor.common import *
from coeditor.retrieval_model import RetrievalEditorModel, AttentionMode
from coeditor.api import (
    EditPredictionService,
    QueryRefEditEncoder,
    BatchArgs,
    DecodingArgs,
)

from jsonrpcserver import Success, method, serve, InvalidParams, Result, Error


def start_server(device, port: int = 5042, print_stats: bool = True):
    model_path = get_model_dir(True) / "coeditor-large-bi-request-stub-v4"
    model = RetrievalEditorModel.load(model_path)
    model.attention_mode = AttentionMode.bidirectional
    model.to(device)
    print(f"Model '{model_path.name}' loaded on device:", device)
    batch_args = BatchArgs.service_default()
    services = dict[Path, EditPredictionService]()

    @method
    def suggestEdits(project: str, file: str, line: int):
        target_dir = Path(project).resolve()
        if (service := services.get(target_dir)) is None:
            service = EditPredictionService(
                target_dir,
                model,
                batch_args=batch_args,
                encoder=QueryRefEditEncoder(
                    max_ref_tks=batch_args.max_ref_tks,
                    max_query_tks=batch_args.max_query_tks,
                    max_output_tks=batch_args.max_output_tks,
                ),
                dec_args=DecodingArgs(do_sample=False, num_beams=8),
                # dec_args=DecodingArgs(
                #     do_sample=True, top_p=0.95, marginalize_samples=20
                # ),
            )
            print(f"Service created for project: {target_dir}")
            services[target_dir] = service

        print(f"Suggesting edit for line {line} in {file}")
        path = Path(file)
        if not Path.is_absolute(path):
            path = target_dir / path
        try:
            response = service.suggest_edit(path, line)
            if print_stats:
                print("Runtime stats:")
                display(service.tlogger.as_dataframe())
            return Success(response.to_json())
        except Exception as e:
            print(e)
            return Error(code=1, message=str(e))

    print(f"Starting suggestion server at localhost:{port}")
    serve("localhost", port)


if __name__ == "__main__":
    start_server("cuda:2")
