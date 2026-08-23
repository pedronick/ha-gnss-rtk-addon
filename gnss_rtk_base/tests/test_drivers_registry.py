import pytest

import drivers


def test_known_drivers_are_registered():
    assert set(drivers.DRIVERS) == {"unicore_um98x", "ublox_zedf9p"}


def test_get_driver_returns_expected_module():
    assert drivers.get_driver("unicore_um98x") is drivers.unicore
    assert drivers.get_driver("ublox_zedf9p") is drivers.ublox


def test_get_driver_raises_on_unknown_name():
    with pytest.raises(ValueError, match="Unsupported"):
        drivers.get_driver("nonexistent_module")


def test_every_driver_exposes_the_required_contract():
    required = ("configure_rtcm", "configure_nmea", "set_rover_mode", "set_fixed_base")
    for name, module in drivers.DRIVERS.items():
        for fn_name in required:
            assert hasattr(module, fn_name), f"{name} is missing {fn_name}"
            assert callable(getattr(module, fn_name))
