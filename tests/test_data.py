"""数据契约：PIT 截断、三时间戳、质量检查。"""

from __future__ import annotations

import pandas as pd

from wealth_quant.data.quality import SEVERITY_ERROR, QualityIssue, QualityReport, run_quality_checks


def test_synthetic_dataset_shape(small_dataset):
    ds = small_dataset
    assert len(ds.instruments) == 6
    assert not ds.bars.empty
    for column in ("event_time", "published_at", "available_at"):
        assert column in ds.bars.columns
        assert ds.bars[column].notna().all()
    # available_at 必须不早于 published_at，published_at 不早于 event_time
    assert (pd.to_datetime(ds.bars["available_at"]) >= pd.to_datetime(ds.bars["published_at"])).all()
    assert (pd.to_datetime(ds.bars["published_at"]) >= pd.to_datetime(ds.bars["event_time"])).all()


def test_as_of_is_point_in_time(small_dataset):
    as_of = pd.Timestamp("2023-06-30 23:59:59")
    cut = small_dataset.as_of(as_of.date())
    assert len(cut.bars) < len(small_dataset.bars)
    assert pd.to_datetime(cut.bars["available_at"]).max() <= as_of
    # 截断后不应包含 as_of 之后的交易日
    assert pd.to_datetime(cut.bars["trade_date"]).max() <= as_of


def test_quality_passes_on_clean_sample(small_dataset):
    report = run_quality_checks(small_dataset, as_of=pd.Timestamp("2024-12-31").date(), max_stale_days=3)
    assert isinstance(report, QualityReport)
    assert report.passed, [i.to_dict() for i in report.errors]


def test_quality_detects_duplicate_bars(small_dataset):
    bars = pd.concat([small_dataset.bars, small_dataset.bars.iloc[[-1]]], ignore_index=True)
    ds = small_dataset.__class__(
        instruments=small_dataset.instruments,
        bars=bars,
        fundamentals=small_dataset.fundamentals,
        corporate_actions=small_dataset.corporate_actions,
        macro=small_dataset.macro,
        calendar=small_dataset.calendar,
    )
    report = run_quality_checks(ds)
    assert any(i.check == "duplicate_bars" for i in report.errors)


def test_quality_report_blocks_on_stale_data():
    report = QualityReport(
        as_of=None,
        issues=[QualityIssue("stale_data", SEVERITY_ERROR, "行情超过 3 个交易日未更新", count=1)],
        checked_rows=0,
        instruments=1,
    )
    assert not report.passed
    assert report.errors[0].check == "stale_data"
