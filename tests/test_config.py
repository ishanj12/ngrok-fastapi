import pytest

from ngrok_fastapi import CollisionError, Config, _validate_configs


def test_single_config_never_raises():
    _validate_configs([Config()])


def test_distinct_urls_never_raise():
    _validate_configs([Config(url="a.ngrok.app"), Config(url="b.ngrok.app")])


def test_two_url_less_configs_without_pooling_raises():
    with pytest.raises(CollisionError, match="account's default dev domain"):
        _validate_configs([Config(), Config()])


def test_two_configs_sharing_a_url_without_pooling_raises():
    with pytest.raises(CollisionError, match="a.ngrok.app"):
        _validate_configs([Config(url="a.ngrok.app"), Config(url="a.ngrok.app")])


def test_pooling_on_every_colliding_entry_does_not_raise():
    _validate_configs([Config(pooling=True), Config(pooling=True)])


def test_pooling_on_only_some_colliding_entries_still_raises():
    with pytest.raises(CollisionError):
        _validate_configs([Config(pooling=True), Config(pooling=False)])
