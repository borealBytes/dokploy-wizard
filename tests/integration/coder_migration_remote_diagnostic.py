from __future__ import annotations

import contextlib
import importlib
import io
import traceback
from pathlib import Path

from tests.integration.coder_migration_remote_diagnostic_result import (
    STAGES,
    RemoteDiagnosticResult,
    result_bytes,
)


def _failure(
    stage: str,
    completed: tuple[str, ...],
    error: BaseException,
) -> RemoteDiagnosticResult:
    return RemoteDiagnosticResult(
        stage=stage,
        completed=completed,
        error_category=_error_category(error),
        error_origin=_error_origin(error),
        error_type=_error_type(error),
        runtime_status="unavailable",
        runtime_phase="unavailable",
        runtime_cause="unavailable",
    )


def _error_category(error: BaseException) -> str:
    match error:
        case ModuleNotFoundError():
            return "module-not-found"
        case ImportError():
            return "import"
        case OSError():
            return "os"
        case RuntimeError():
            return "runtime"
        case SyntaxError():
            return "syntax"
        case TypeError():
            return "type"
        case ValueError():
            return "value"
        case AttributeError():
            return "attribute"
        case SystemExit():
            return "system-exit"
        case _:
            return "unexpected"


def _error_origin(error: BaseException) -> str:
    entries = traceback.extract_tb(error.__traceback__)
    if not entries:
        return "unknown.py:0:unknown"
    entry = entries[-1]
    function = entry.name if entry.name.isidentifier() else "module"
    return f"{Path(entry.filename).name}:{entry.lineno}:{function}"


def _error_type(error: BaseException) -> str:
    name = type(error).__name__
    return name if name.isascii() and name.isidentifier() else "UnexpectedError"


def diagnose() -> RemoteDiagnosticResult:
    completed: tuple[str, ...] = ()
    try:
        importlib.import_module("dokploy_wizard")
    except (
        ImportError,
        OSError,
        RuntimeError,
        SyntaxError,
        TypeError,
        ValueError,
        AttributeError,
        SystemExit,
    ) as error:
        return _failure("package-import", completed, error)
    completed += (STAGES[0],)
    try:
        from tests.integration.coder_migration_remote_docker import RemoteCoderStack
        from tests.integration.coder_migration_remote_main import (
            _emit,
            _images,
            run,
        )
        from tests.integration.coder_migration_remote_result import (
            parse_runtime_result,
            result_bytes,
        )
    except (
        ImportError,
        OSError,
        RuntimeError,
        SyntaxError,
        TypeError,
        ValueError,
        AttributeError,
        SystemExit,
    ) as error:
        return _failure("module-import", completed, error)
    completed += (STAGES[1],)
    try:
        images = _images()
    except (
        ImportError,
        OSError,
        RuntimeError,
        SyntaxError,
        TypeError,
        ValueError,
        AttributeError,
        SystemExit,
    ) as error:
        return _failure("images", completed, error)
    completed += (STAGES[2],)
    try:
        RemoteCoderStack(images)
    except (
        ImportError,
        OSError,
        RuntimeError,
        SyntaxError,
        TypeError,
        ValueError,
        AttributeError,
        SystemExit,
    ) as error:
        return _failure("stack-construction", completed, error)
    completed += (STAGES[3],)
    try:
        runtime_result = run()
    except (
        ImportError,
        OSError,
        RuntimeError,
        SyntaxError,
        TypeError,
        ValueError,
        AttributeError,
        SystemExit,
    ) as error:
        return _failure("run", completed, error)
    completed += (STAGES[4],)
    try:
        serialized = result_bytes(runtime_result)
        parse_runtime_result(serialized)
    except (
        ImportError,
        OSError,
        RuntimeError,
        SyntaxError,
        TypeError,
        ValueError,
        AttributeError,
        SystemExit,
    ) as error:
        return _failure("serialization", completed, error)
    completed += (STAGES[5],)
    try:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = _emit(runtime_result)
        if exit_code != 0 or output.getvalue() != serialized.decode("utf-8") + "\n":
            raise RuntimeError("remote entrypoint serialized an unexpected result")
    except (
        ImportError,
        OSError,
        RuntimeError,
        SyntaxError,
        TypeError,
        ValueError,
        AttributeError,
        SystemExit,
    ) as error:
        return _failure("entrypoint", completed, error)
    return RemoteDiagnosticResult(
        stage="entrypoint",
        completed=(*completed, STAGES[6]),
        error_category="none",
        error_origin="none",
        error_type="none",
        runtime_status=runtime_result.status,
        runtime_phase=runtime_result.phase,
        runtime_cause=runtime_result.cause,
    )


def main() -> int:
    print(result_bytes(diagnose()).decode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
