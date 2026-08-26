# -*- coding: utf-8 -*-
"""test_contract.py：experiment_contract.yaml schema 校验器（§5.2）。"""
import pytest

from minifars.contract import (ContractError, contract_skeleton,
                               extract_json_object, flatten_tasks,
                               load_contract, save_contract, validate_contract)


def test_skeleton_passes_validation():
    assert validate_contract(contract_skeleton()) == []


def test_baselines_below_two_rejected():
    c = contract_skeleton()
    c["tasks"]["baselines"] = c["tasks"]["baselines"][:1]
    problems = validate_contract(c)
    assert any("baselines" in p for p in problems)


def test_analysis_below_one_rejected():
    c = contract_skeleton()
    c["tasks"]["analysis"] = []
    problems = validate_contract(c)
    assert any("analysis" in p for p in problems)


def test_missing_main_rejected():
    c = contract_skeleton()
    c["tasks"]["main"] = []
    assert any("main" in p for p in validate_contract(c))


def test_duplicate_task_id_rejected():
    c = contract_skeleton()
    c["tasks"]["analysis"].append(dict(c["tasks"]["analysis"][0], id="B1"))
    assert any("重复" in p for p in validate_contract(c))


def test_task_missing_required_fields_rejected():
    c = contract_skeleton()
    c["tasks"]["baselines"][0] = {"id": "B1"}  # 缺 name/method/metric
    assert any("缺字段" in p for p in validate_contract(c))


def test_gate_bad_direction_rejected():
    c = contract_skeleton()
    c["tasks"]["effectiveness_gate"]["direction"] = "ge"
    assert any("direction" in p for p in validate_contract(c))


def test_gate_missing_fields_rejected():
    c = contract_skeleton()
    del c["tasks"]["effectiveness_gate"]["threshold"]
    assert any("effectiveness_gate" in p for p in validate_contract(c))


def test_unknown_metric_rejected():
    c = contract_skeleton()
    c["tasks"]["main"][0]["metric"] = "made_up_metric"
    c["tasks"]["effectiveness_gate"]["metric"] = "M1.made_up_metric"
    problems = validate_contract(c)
    assert any("made_up_metric" in p for p in problems)


def test_semantic_metric_alias_accepted():
    c = contract_skeleton()
    c["tasks"]["main"][0]["metric"] = "recall_security"  # 别名表内
    assert not any("recall_security" in p for p in validate_contract(c))


def test_unknown_method_rejected():
    c = contract_skeleton()
    c["tasks"]["main"][0]["method"] = "hallucinated_method"
    assert any("hallucinated_method" in p for p in validate_contract(c))


def test_threshold_must_be_numeric():
    c = contract_skeleton()
    c["tasks"]["effectiveness_gate"]["threshold"] = "abc"
    assert any("threshold" in p for p in validate_contract(c))


def test_seeds_must_be_int_list():
    c = contract_skeleton()
    c["tasks"]["main"][0]["seeds"] = ["x"]
    assert any("seeds" in p for p in validate_contract(c))
    c["tasks"]["main"][0]["seeds"] = [True]  # bool 是 int 子类，显式排除
    assert any("seeds" in p for p in validate_contract(c))


def test_budget_must_be_positive_ints():
    c = contract_skeleton()
    c["budget"]["llm_tokens_total"] = -1
    c["budget"]["per_task_timeout_s"] = "60"
    problems = validate_contract(c)
    assert any("llm_tokens_total" in p for p in problems)
    assert any("per_task_timeout_s" in p for p in problems)


def test_empty_hypothesis_and_predict_rejected():
    c = contract_skeleton()
    c["hypothesis"], c["predict"] = "", "   "
    problems = validate_contract(c)
    assert any("hypothesis" in p for p in problems)
    assert any("predict" in p for p in problems)


def test_tasks_must_be_mapping():
    c = contract_skeleton()
    c["tasks"] = []
    assert any("映射" in p for p in validate_contract(c))


def test_flatten_tasks_order_and_type():
    flat = flatten_tasks(contract_skeleton())
    types = [t["task_type"] for t in flat]
    # effectiveness_gate 不在执行序列；顺序 env → baselines → main → analysis
    assert types == ["env_setup", "baselines", "baselines", "main", "analysis"]
    assert [t["id"] for t in flat] == ["E1", "B1", "B2", "M1", "A1"]


def test_save_and_load_roundtrip(tmp_path):
    c = contract_skeleton()
    path = save_contract(c, tmp_path / "plan" / "experiment_contract.yaml")
    loaded = load_contract(path)
    assert loaded["hypothesis"] == c["hypothesis"]
    assert loaded["tasks"]["main"][0]["id"] == "M1"


def test_load_contract_missing_raises(tmp_path):
    with pytest.raises(ContractError):
        load_contract(tmp_path / "nope.yaml")


def test_load_contract_invalid_raises(tmp_path):
    c = contract_skeleton()
    c["tasks"]["baselines"] = []
    path = save_contract(c, tmp_path / "bad.yaml")
    with pytest.raises(ContractError):
        load_contract(path)


def test_extract_json_object_fenced():
    text = '前言\n```json\n{"hypothesis": "H"}\n```\n后记'
    assert extract_json_object(text) == {"hypothesis": "H"}


def test_extract_json_object_bare_and_noise():
    assert extract_json_object('noise {"a": 1} noise') == {"a": 1}
    assert extract_json_object("没有 JSON") == {}
    assert extract_json_object("{坏 json") == {}
    assert extract_json_object("[1, 2]") == {}  # 数组不是对象
