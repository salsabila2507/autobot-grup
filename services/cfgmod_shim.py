import os

from engine import config

cfg = None


def load(path="config.yaml"):
    global cfg
    cfg = config.load(path)
    return cfg


def read_secret(c, name):
    return config.read_secret(c, name)
