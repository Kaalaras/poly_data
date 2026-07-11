from __future__ import annotations

import json
from pathlib import Path

import pytest

from poly_data.cli import main


def test_cli_help_returns_zero(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "update-all" in out
    assert "import-ponder-v2" in out
    assert "download-v2-logs" in out
    assert "benchmark-polygon-rpc" in out
    assert "compact" in out
    assert "v2-status" in out
    assert "validate" in out
    for removed in ("update-" + "gold" + "sky", "serve-v2-" + "web" + "hook"):
        assert removed not in out


def test_cli_compact_invokes_compact_all(tmp_path: Path, mocker) -> None:
    fake = mocker.patch("poly_data.cli.compact_all", return_value={})
    code = main([
        "compact",
        "--data-root", str(tmp_path / "data"),
        "--source", "orderFilled",
    ])
    assert code == 0
    fake.assert_called_once()
    args, _ = fake.call_args
    assert args[1] == "orderFilled"


def test_cli_compact_due_invokes_due_compaction(tmp_path: Path, mocker) -> None:
    fake = mocker.patch("poly_data.cli.compact_due", return_value={})
    code = main([
        "compact", "--data-root", str(tmp_path / "data"), "--source", "trades", "--due",
    ])

    assert code == 0
    fake.assert_called_once()


def test_cli_update_all_runs_pipeline(tmp_path: Path, mocker) -> None:
    mocker.patch("poly_data.cli.update_markets", return_value=0)
    output = tmp_path / "fills.jsonl"
    download = mocker.patch(
        "poly_data.cli.download_v2_logs",
        return_value=mocker.Mock(output_path=output, rows=2, ranges=1),
    )
    importer = mocker.patch("poly_data.cli.import_ponder_v2_jsonl", return_value=2)
    discover = mocker.patch("poly_data.cli._discover_and_fetch_missing_tokens", return_value=0)
    refresh = mocker.patch("poly_data.cli.refresh_market_dimensions", return_value={})
    process = mocker.patch("poly_data.cli.process_trades", return_value=0)
    code = main(["update-all", "--data-root", str(tmp_path / "data")])
    assert code == 0
    download.assert_called_once()
    importer.assert_called_once_with(output, store=mocker.ANY)
    discover.assert_called_once()
    _, discover_kwargs = discover.call_args
    assert discover_kwargs["source"] == "v2"
    refresh.assert_called_once()
    process.assert_called_once()
    _, process_kwargs = process.call_args
    assert process_kwargs["source"] == "v2"


def test_cli_update_all_rejects_ponder_as_a_production_source(tmp_path: Path, mocker) -> None:
    jsonl = tmp_path / "fills.jsonl"
    jsonl.write_text("", encoding="utf-8")
    mocker.patch("poly_data.cli.update_markets", return_value=0)
    with pytest.raises(SystemExit) as exc:
        main([
            "update-all",
            "--data-root", str(tmp_path / "data"),
            "--ponder-jsonl", str(jsonl),
        ])
    assert exc.value.code == 2


def test_cli_import_ponder_v2_invokes_importer(tmp_path: Path, mocker) -> None:
    jsonl = tmp_path / "fills.jsonl"
    jsonl.write_text("", encoding="utf-8")
    importer = mocker.patch("poly_data.cli.import_ponder_v2_jsonl", return_value=2)
    code = main([
        "import-ponder-v2",
        "--data-root", str(tmp_path / "data"),
        str(jsonl),
        "--batch-size", "17",
    ])
    assert code == 0
    importer.assert_called_once()
    _, kwargs = importer.call_args
    assert kwargs["batch_size"] == 17


def test_cli_download_v2_logs_invokes_downloader(tmp_path: Path, mocker) -> None:
    fake = mocker.patch(
        "poly_data.cli.download_v2_logs",
        return_value=mocker.Mock(rows=3, ranges=1, output_path=tmp_path / "fills.jsonl"),
    )
    code = main([
        "download-v2-logs",
        "--data-root", str(tmp_path / "data"),
        "--rpc-url", "https://example.test/rpc",
        "--from-block", "10",
        "--to-block", "20",
        "--chunk-size", "5",
        "--confirmations", "64",
        "--overlap-blocks", "32",
        "--limit-ranges", "1",
    ])
    assert code == 0
    fake.assert_called_once()
    _, kwargs = fake.call_args
    assert kwargs["rpc_url"] == "https://example.test/rpc"
    assert kwargs["from_block"] == 10
    assert kwargs["to_block"] == 20
    assert kwargs["chunk_size"] == 5
    assert kwargs["confirmations"] == 64
    assert kwargs["overlap_blocks"] == 32
    assert kwargs["limit_ranges"] == 1


def test_cli_benchmark_polygon_rpc_prints_json(tmp_path: Path, mocker, capsys) -> None:
    fake = mocker.patch(
        "poly_data.cli.benchmark_polygon_rpc",
        return_value=[{"endpoint": "https://example.test", "recommended_chunk_size": 1000}],
    )
    code = main([
        "benchmark-polygon-rpc",
        "--data-root", str(tmp_path / "data"),
        "--rpc-url", "https://example.test/rpc",
        "--span", "50",
    ])
    assert code == 0
    _, kwargs = fake.call_args
    assert kwargs["rpc_urls"] == ["https://example.test/rpc"]
    assert kwargs["spans"] == (50,)
    assert '"recommended_chunk_size": 1000' in capsys.readouterr().out


def test_cli_benchmark_lake_prints_json(tmp_path: Path, mocker, capsys) -> None:
    mocker.patch(
        "poly_data.cli.benchmark_source",
        return_value={"rows": 2, "files": 1, "bytes": 100, "seconds": 0.1},
    )

    code = main([
        "benchmark-lake", "--data-root", str(tmp_path / "data"), "--source", "trades",
    ])

    assert code == 0
    assert '"rows": 2' in capsys.readouterr().out


def test_cli_process_v2_discovers_v2_missing_markets(tmp_path: Path, mocker) -> None:
    discover = mocker.patch("poly_data.cli._discover_and_fetch_missing_tokens", return_value=0)
    refresh = mocker.patch("poly_data.cli.refresh_market_dimensions", return_value={})
    process = mocker.patch("poly_data.cli.process_trades", return_value=3)
    code = main([
        "process",
        "--data-root", str(tmp_path / "data"),
        "--source", "v2",
    ])
    assert code == 0
    discover.assert_called_once()
    _, discover_kwargs = discover.call_args
    assert discover_kwargs["source"] == "v2"
    refresh.assert_called_once()
    process.assert_called_once()
    _, kwargs = process.call_args
    assert kwargs["source"] == "v2"


def test_cli_refresh_market_dimensions(tmp_path: Path, mocker) -> None:
    refresh = mocker.patch(
        "poly_data.cli.refresh_market_dimensions",
        return_value={"market_assets": 2, "markets_current": 1},
    )

    code = main(["refresh-market-dimensions", "--data-root", str(tmp_path / "data")])

    assert code == 0
    refresh.assert_called_once()


def test_cli_v2_status_prints_json(tmp_path: Path, mocker, capsys) -> None:
    mocker.patch("poly_data.cli.build_v2_status", return_value={"raw_v2_rows": 1})
    code = main(["v2-status", "--data-root", str(tmp_path / "data")])
    assert code == 0
    assert '"raw_v2_rows": 1' in capsys.readouterr().out


def test_cli_validate_prints_json_for_selected_source(tmp_path: Path, capsys) -> None:
    code = main([
        "validate",
        "--data-root", str(tmp_path / "data"),
        "--source", "trades",
    ])

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["status"] == "warning"
    assert list(payload["sources"]) == ["trades"]


def test_cli_validate_strict_fails_on_warning(tmp_path: Path, capsys) -> None:
    code = main([
        "validate",
        "--data-root", str(tmp_path / "data"),
        "--source", "trades",
        "--strict",
    ])

    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["status"] == "warning"
