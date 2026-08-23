from quant.analyze.scoring import label_100, to_100


def test_zero_raw_is_neutral():
    assert to_100(0, 5) == 50


def test_max_raw_is_100():
    assert to_100(5, 5) == 100


def test_min_raw_is_0():
    assert to_100(-5, 5) == 0


def test_partial_positive():
    assert to_100(3, 5) == 80


def test_partial_negative():
    assert to_100(-2, 5) == 30


def test_out_of_range_clamps_to_100():
    assert to_100(99, 5) == 100


def test_out_of_range_clamps_to_0():
    assert to_100(-99, 5) == 0


def test_zero_span_defends_to_neutral():
    assert to_100(1, 0) == 50


def test_label_100_boundaries():
    assert label_100(75, "긍정 신호", "부정 신호") == "강한 긍정 신호"
    assert label_100(60, "긍정 신호", "부정 신호") == "약한 긍정 신호"
    assert label_100(50, "긍정 신호", "부정 신호") == "중립"
    assert label_100(40, "긍정 신호", "부정 신호") == "약한 부정 신호"
    assert label_100(25, "긍정 신호", "부정 신호") == "강한 부정 신호"
