# Copyright (c) 2016-present, Facebook, Inc.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import unittest
from typing import Callable

from ..get_REST_api_sources import RESTApiSourceGenerator


class GetRESTApiSourcesTest(unittest.TestCase):
    def test_compute_models(self):
        def testA():
            pass

        def testB(x):
            pass

        def testC(x: int):
            pass

        def testD(x: int, *args: int):
            pass

        def testE(x: int, **kwargs: str):
            pass

        class TestClass:
            def methodA(self, x: int):
                ...

            def methodB(self, *args: str):
                ...

        all_views = [
            testA,
            testB,
            testC,
            testD,
            testE,
            TestClass.methodA,
            TestClass.methodB,
        ]
        qualifier = f"{__name__}.GetRESTApiSourcesTest.test_compute_models"
        source = "TaintSource[UserControlled]"
        self.assertEqual(
            list(RESTApiSourceGenerator([], []).compute_models(all_views)),
            [
                f"def {qualifier}.TestClass.methodA(self: {source}, x: {source}): ...",
                f"def {qualifier}.TestClass.methodB(self: {source}, *args: {source})"
                ": ...",
                f"def {qualifier}.testA(): ...",
                f"def {qualifier}.testB(x: {source}): ...",
                f"def {qualifier}.testC(x: {source}): ...",
                f"def {qualifier}.testD(x: {source}, *args: {source}): ...",
                f"def {qualifier}.testE(x: {source}, **kwargs: {source}): ...",
            ],
        )
        self.assertEqual(
            list(
                RESTApiSourceGenerator(["int"], [f"{qualifier}.testA"]).compute_models(
                    all_views
                )
            ),
            [
                f"def {qualifier}.TestClass.methodA(self: {source}, x): ...",
                f"def {qualifier}.TestClass.methodB(self: {source}, *args: {source})"
                ": ...",
                f"def {qualifier}.testB(x: {source}): ...",
                f"def {qualifier}.testC(x): ...",
                f"def {qualifier}.testD(x, *args): ...",
                f"def {qualifier}.testE(x, **kwargs: {source}): ...",
            ],
        )
