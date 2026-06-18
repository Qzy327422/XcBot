import sys
import os

try:
    import Hyper
    print("Hyper path:", Hyper.__file__)
except Exception as e:
    print("Failed:", e)
