from quant.collect.sources.fred import curve_label, net_liquidity, nfci_label


def test_nfci_label_reads_the_sign():
    """NFCI 는 0 이 장기평균 — 숫자만 보면 완화인지 긴축인지 알 수 없다."""
    assert nfci_label(-0.529) == "완화"
    assert nfci_label(0.0) == "중립"
    assert nfci_label(0.5) == "긴축"


def test_nfci_label_boundaries():
    assert nfci_label(-0.2) == "완화"
    assert nfci_label(-0.19) == "중립"
    assert nfci_label(0.19) == "중립"
    assert nfci_label(0.2) == "긴축"


def test_curve_label_flags_inversion():
    assert curve_label(-0.3) == "역전"
    assert curve_label(0.0) == "평탄"
    assert curve_label(0.47) == "평탄"
    assert curve_label(1.2) == "정상"


def test_net_liquidity_is_walcl_minus_tga_minus_rrp():
    assert net_liquidity(6_748_567, 907_324, 1) == 5_841_242
