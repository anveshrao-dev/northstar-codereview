"""Offline tests: accidental networking fails instead of contacting a service."""

import socket

import pytest


@pytest.fixture(autouse=True)
def offline_test(monkeypatch):
    attempts = []
    def deny_network(*args, **kwargs):
        attempts.append(True)
        raise AssertionError("Network access is forbidden in project tests")

    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")
    monkeypatch.setattr(socket.socket, "connect", deny_network)
    monkeypatch.setattr(socket.socket, "connect_ex", deny_network)
    monkeypatch.setattr(socket, "create_connection", deny_network)
    monkeypatch.setattr(socket, "getaddrinfo", deny_network)
    yield
    # A production error handler must not hide an accidental test network attempt.
    assert not attempts, "A test attempted network access, even if its error was caught"
