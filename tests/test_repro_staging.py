import json
from pathlib import Path

import pandas as pd
import pytest

from uq.errors import ContractError
from uq.factors.raw_price import calculate_raw_price_factors, logical_fingerprint
from uq.factors.repro_staging import ReproStaging, assert_reproducible, environment_profile


def factor_frame():
    rows=[{
        "instrument":"600000.XSHG","datetime":pd.Timestamp(2026,8,day),"high":11.0,"low":9.0,
        "close":10.0,"volume":float(day),"amount":float(day),
    } for day in range(1,22)]
    return calculate_raw_price_factors(pd.DataFrame(rows))


def test_environment_profile_is_complete():
    profile=environment_profile()
    for field in ("python_version","pandas_version","pyarrow_version","numpy_version","blas_backend","os_family","cpu_architecture","lockfile_sha256"):
        assert field in profile


def test_repeated_staging_outputs_are_byte_and_logically_identical(tmp_path):
    frame=factor_frame(); fingerprint=logical_fingerprint(frame)
    left=ReproStaging().write(tmp_path,frame,fingerprint)
    right=ReproStaging().write(tmp_path,frame,fingerprint)
    assert left.artifact_checksum==right.artifact_checksum
    assert_reproducible(left,right)


def test_mismatch_cannot_be_promoted(tmp_path):
    frame=factor_frame()
    left=ReproStaging().write(tmp_path,frame,logical_fingerprint(frame))
    right=ReproStaging().write(tmp_path,frame,"f"*64)
    with pytest.raises(ContractError,match="logical fingerprint mismatch"):
        assert_reproducible(left,right)
    assert not (tmp_path / "factors").exists()


def test_staging_is_physically_isolated_from_accepted_paths(tmp_path):
    run=ReproStaging().write(tmp_path,factor_frame(),"a"*64)
    assert "repro_staging" in run.directory.parts
    assert not (tmp_path / "factors").exists()
    accepted_candidates=list((tmp_path / "factors").glob("**/data.parquet")) if (tmp_path/"factors").exists() else []
    assert accepted_candidates==[]
