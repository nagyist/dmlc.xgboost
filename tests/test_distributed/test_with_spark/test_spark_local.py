import glob
import logging
import random
import tempfile
import uuid
from collections import namedtuple
from typing import Generator, Iterable, List, Sequence

import numpy as np
import pytest
from pyspark import SparkConf
from pyspark.ml import Pipeline, PipelineModel
from pyspark.ml.evaluation import BinaryClassificationEvaluator
from pyspark.ml.feature import VectorAssembler
from pyspark.ml.functions import vector_to_array
from pyspark.ml.linalg import Vectors
from pyspark.ml.tuning import CrossValidator, ParamGridBuilder
from pyspark.sql import SparkSession
from pyspark.sql import functions as spark_sql_func

import xgboost as xgb
from xgboost import XGBClassifier, XGBModel, XGBRegressor
from xgboost import testing as tm
from xgboost.collective import Config
from xgboost.spark import (
    SparkXGBClassifier,
    SparkXGBClassifierModel,
    SparkXGBRanker,
    SparkXGBRegressor,
    SparkXGBRegressorModel,
)
from xgboost.spark.core import _non_booster_params
from xgboost.spark.data import pred_contribs
from xgboost.testing.collective import get_avail_port

from .utils import SparkTestCase

logging.getLogger("py4j").setLevel(logging.INFO)

pytestmark = [tm.timeout(60), pytest.mark.skipif(**tm.no_spark())]


def no_sparse_unwrap() -> tm.PytestSkip:
    try:
        from pyspark.sql.functions import unwrap_udt

    except ImportError:
        return {"reason": "PySpark<3.4", "condition": True}

    return {"reason": "PySpark<3.4", "condition": False}


@pytest.fixture
def spark() -> Generator[SparkSession, None, None]:
    config = {
        "spark.master": "local[4]",
        "spark.python.worker.reuse": "false",
        "spark.driver.host": "127.0.0.1",
        "spark.task.maxFailures": "1",
        "spark.sql.execution.pyspark.udf.simplifiedTraceback.enabled": "false",
        "spark.sql.pyspark.jvmStacktrace.enabled": "true",
    }

    builder = SparkSession.builder.appName("XGBoost PySpark Python API Tests")
    for k, v in config.items():
        builder.config(k, v)
    logging.getLogger("pyspark").setLevel(logging.INFO)
    sess = builder.getOrCreate()
    yield sess

    sess.stop()
    sess.sparkContext.stop()


RegWithWeight = namedtuple(
    "RegWithWeight",
    (
        "reg_params_with_eval",
        "reg_df_train_with_eval_weight",
        "reg_df_test_with_eval_weight",
        "reg_with_eval_best_score",
        "reg_with_eval_and_weight_best_score",
        "reg_expected_evals_result_train",
        "reg_expected_evals_result_validation",
    ),
)


@pytest.fixture
def reg_with_weight(
    spark: SparkSession,
) -> Generator[RegWithWeight, SparkSession, None]:
    reg_params_with_eval = {
        "validation_indicator_col": "isVal",
        "early_stopping_rounds": 1,
        "eval_metric": "rmse",
    }

    X = np.array([[1.0, 2.0, 3.0], [0.0, 1.0, 5.5], [4.0, 5.0, 6.0], [0.0, 6.0, 7.5]])
    w = np.array([1.0, 2.0, 1.0, 2.0])
    y = np.array([0, 1, 2, 3])

    reg1 = XGBRegressor()
    reg1.fit(X, y, sample_weight=w)
    predt1 = reg1.predict(X)

    X_train = np.array([[1.0, 2.0, 3.0], [0.0, 1.0, 5.5]])
    X_val = np.array([[4.0, 5.0, 6.0], [0.0, 6.0, 7.5]])
    y_train = np.array([0, 1])
    y_val = np.array([2, 3])
    w_train = np.array([1.0, 2.0])
    w_val = np.array([1.0, 2.0])

    reg2 = XGBRegressor(early_stopping_rounds=1, eval_metric="rmse")
    reg2.fit(
        X_train,
        y_train,
        eval_set=[(X_val, y_val)],
    )
    predt2 = reg2.predict(X)
    best_score2 = reg2.best_score

    reg3 = XGBRegressor(early_stopping_rounds=1, eval_metric="rmse")
    reg3.fit(
        X_train,
        y_train,
        sample_weight=w_train,
        eval_set=[(X_val, y_val)],
        sample_weight_eval_set=[w_val],
    )
    predt3 = reg3.predict(X)
    best_score3 = reg3.best_score

    reg4 = XGBRegressor(eval_metric="rmse")
    reg4.fit(X_train, y_train, eval_set=[(X_train, y_train), (X_val, y_val)])
    reg_expected_evals_result = reg4.evals_result()
    reg_expected_evals_result_train = reg_expected_evals_result["validation_0"]["rmse"]
    reg_expected_evals_result_validation = reg_expected_evals_result["validation_1"][
        "rmse"
    ]

    reg_df_train_with_eval_weight = spark.createDataFrame(
        [
            (Vectors.dense(1.0, 2.0, 3.0), 0, False, 1.0),
            (Vectors.sparse(3, {1: 1.0, 2: 5.5}), 1, False, 2.0),
            (Vectors.dense(4.0, 5.0, 6.0), 2, True, 1.0),
            (Vectors.sparse(3, {1: 6.0, 2: 7.5}), 3, True, 2.0),
        ],
        ["features", "label", "isVal", "weight"],
    )

    reg_df_test_with_eval_weight = spark.createDataFrame(
        [
            (
                Vectors.dense(1.0, 2.0, 3.0),
                float(predt1[0]),
                float(predt2[0]),
                float(predt3[0]),
            ),
            (
                Vectors.sparse(3, {1: 1.0, 2: 5.5}),
                float(predt1[1]),
                float(predt2[1]),
                float(predt3[1]),
            ),
        ],
        [
            "features",
            "expected_prediction_with_weight",
            "expected_prediction_with_eval",
            "expected_prediction_with_weight_and_eval",
        ],
    )
    yield RegWithWeight(
        reg_params_with_eval,
        reg_df_train_with_eval_weight,
        reg_df_test_with_eval_weight,
        best_score2,
        best_score3,
        reg_expected_evals_result_train,
        reg_expected_evals_result_validation,
    )


RegData = namedtuple("RegData", ("reg_df_train", "reg_df_test", "reg_params"))


@pytest.fixture
def reg_data(spark: SparkSession) -> Generator[RegData, None, None]:
    X = np.array([[1.0, 2.0, 3.0], [0.0, 1.0, 5.5]])
    y = np.array([0, 1])
    reg1 = xgb.XGBRegressor()
    reg1.fit(X, y)
    predt0 = reg1.predict(X)
    pred_contrib0: np.ndarray = pred_contribs(reg1, X, None, False)

    reg_params = {
        "max_depth": 5,
        "n_estimators": 10,
        "iteration_range": [0, 5],
        "max_bin": 9,
    }

    # convert np array to pyspark dataframe
    reg_df_train_data = [
        (Vectors.dense(X[0, :]), int(y[0])),
        (Vectors.sparse(3, {1: float(X[1, 1]), 2: float(X[1, 2])}), int(y[1])),
    ]
    reg_df_train = spark.createDataFrame(reg_df_train_data, ["features", "label"])

    reg2 = xgb.XGBRegressor(max_depth=5, n_estimators=10)
    reg2.fit(X, y)
    predt2 = reg2.predict(X, iteration_range=[0, 5])
    # array([0.22185266, 0.77814734], dtype=float32)

    reg_df_test = spark.createDataFrame(
        [
            (
                Vectors.dense(X[0, :]),
                float(predt0[0]),
                pred_contrib0[0, :].tolist(),
                float(predt2[0]),
            ),
            (
                Vectors.sparse(3, {1: 1.0, 2: 5.5}),
                float(predt0[1]),
                pred_contrib0[1, :].tolist(),
                float(predt2[1]),
            ),
        ],
        [
            "features",
            "expected_prediction",
            "expected_pred_contribs",
            "expected_prediction_with_params",
        ],
    )
    yield RegData(reg_df_train, reg_df_test, reg_params)


MultiClfData = namedtuple("MultiClfData", ("multi_clf_df_train", "multi_clf_df_test"))


@pytest.fixture
def multi_clf_data(spark: SparkSession) -> Generator[MultiClfData, None, None]:
    X = np.array([[1.0, 2.0, 3.0], [1.0, 2.0, 4.0], [0.0, 1.0, 5.5], [-1.0, -2.0, 1.0]])
    y = np.array([0, 0, 1, 2])
    cls1 = xgb.XGBClassifier()
    cls1.fit(X, y)
    predt0 = cls1.predict(X)
    proba0: np.ndarray = cls1.predict_proba(X)
    pred_contrib0: np.ndarray = pred_contribs(cls1, X, None, False)

    # convert np array to pyspark dataframe
    multi_cls_df_train_data = [
        (Vectors.dense(X[0, :]), int(y[0])),
        (Vectors.dense(X[1, :]), int(y[1])),
        (Vectors.sparse(3, {1: float(X[2, 1]), 2: float(X[2, 2])}), int(y[2])),
        (Vectors.dense(X[3, :]), int(y[3])),
    ]
    multi_clf_df_train = spark.createDataFrame(
        multi_cls_df_train_data, ["features", "label"]
    )

    multi_clf_df_test = spark.createDataFrame(
        [
            (
                Vectors.dense(X[0, :]),
                float(predt0[0]),
                proba0[0, :].tolist(),
                pred_contrib0[0, :].tolist(),
            ),
            (
                Vectors.dense(X[1, :]),
                float(predt0[1]),
                proba0[1, :].tolist(),
                pred_contrib0[1, :].tolist(),
            ),
            (
                Vectors.sparse(3, {1: 1.0, 2: 5.5}),
                float(predt0[2]),
                proba0[2, :].tolist(),
                pred_contrib0[2, :].tolist(),
            ),
        ],
        [
            "features",
            "expected_prediction",
            "expected_probability",
            "expected_pred_contribs",
        ],
    )
    yield MultiClfData(multi_clf_df_train, multi_clf_df_test)


ClfWithWeight = namedtuple(
    "ClfWithWeight",
    (
        "cls_params_with_eval",
        "cls_df_train_with_eval_weight",
        "cls_df_test_with_eval_weight",
        "cls_with_eval_best_score",
        "cls_with_eval_and_weight_best_score",
        "cls_expected_evals_result_train",
        "cls_expected_evals_result_validation",
    ),
)


@pytest.fixture
def clf_with_weight(
    spark: SparkSession,
) -> Generator[ClfWithWeight, SparkSession, None]:
    """Test classifier with weight and eval set."""

    X = np.array([[1.0, 2.0, 3.0], [0.0, 1.0, 5.5], [4.0, 5.0, 6.0], [0.0, 6.0, 7.5]])
    w = np.array([1.0, 2.0, 1.0, 2.0])
    y = np.array([0, 1, 0, 1])
    cls1 = XGBClassifier()
    cls1.fit(X, y, sample_weight=w)

    X_train = np.array([[1.0, 2.0, 3.0], [0.0, 1.0, 5.5]])
    X_val = np.array([[4.0, 5.0, 6.0], [0.0, 6.0, 7.5]])
    y_train = np.array([0, 1])
    y_val = np.array([0, 1])
    w_train = np.array([1.0, 2.0])
    w_val = np.array([1.0, 2.0])
    cls2 = XGBClassifier(eval_metric="logloss", early_stopping_rounds=1)
    cls2.fit(
        X_train,
        y_train,
        eval_set=[(X_val, y_val)],
    )

    cls3 = XGBClassifier(eval_metric="logloss", early_stopping_rounds=1)
    cls3.fit(
        X_train,
        y_train,
        sample_weight=w_train,
        eval_set=[(X_val, y_val)],
        sample_weight_eval_set=[w_val],
    )

    cls4 = XGBClassifier(eval_metric="logloss")
    cls4.fit(
        X_train,
        y_train,
        eval_set=[(X_train, y_train), (X_val, y_val)],
    )
    cls4_evals_result = cls4.evals_result()
    cls_expected_evals_result_train = cls4_evals_result["validation_0"]["logloss"]
    cls_expected_evals_result_validation = cls4_evals_result["validation_1"]["logloss"]

    cls_df_train_with_eval_weight = spark.createDataFrame(
        [
            (Vectors.dense(1.0, 2.0, 3.0), 0, False, 1.0),
            (Vectors.sparse(3, {1: 1.0, 2: 5.5}), 1, False, 2.0),
            (Vectors.dense(4.0, 5.0, 6.0), 0, True, 1.0),
            (Vectors.sparse(3, {1: 6.0, 2: 7.5}), 1, True, 2.0),
        ],
        ["features", "label", "isVal", "weight"],
    )
    cls_params_with_eval = {
        "validation_indicator_col": "isVal",
        "early_stopping_rounds": 1,
        "eval_metric": "logloss",
    }
    cls_df_test_with_eval_weight = spark.createDataFrame(
        [
            (
                Vectors.dense(1.0, 2.0, 3.0),
                [float(p) for p in cls1.predict_proba(X)[0, :]],
                [float(p) for p in cls2.predict_proba(X)[0, :]],
                [float(p) for p in cls3.predict_proba(X)[0, :]],
            ),
        ],
        [
            "features",
            "expected_prob_with_weight",
            "expected_prob_with_eval",
            "expected_prob_with_weight_and_eval",
        ],
    )
    cls_with_eval_best_score = cls2.best_score
    cls_with_eval_and_weight_best_score = cls3.best_score
    yield ClfWithWeight(
        cls_params_with_eval,
        cls_df_train_with_eval_weight,
        cls_df_test_with_eval_weight,
        cls_with_eval_best_score,
        cls_with_eval_and_weight_best_score,
        cls_expected_evals_result_train,
        cls_expected_evals_result_validation,
    )


ClfData = namedtuple(
    "ClfData", ("cls_params", "cls_df_train", "cls_df_train_large", "cls_df_test")
)


@pytest.fixture
def clf_data(spark: SparkSession) -> Generator[ClfData, None, None]:
    cls_params = {"max_depth": 5, "n_estimators": 10, "scale_pos_weight": 4}

    X = np.array([[1.0, 2.0, 3.0], [0.0, 1.0, 5.5]])
    y = np.array([0, 1])
    cl1 = xgb.XGBClassifier()
    cl1.fit(X, y)
    predt0 = cl1.predict(X)
    proba0: np.ndarray = cl1.predict_proba(X)
    pred_contrib0: np.ndarray = pred_contribs(cl1, X, None, True)
    cl2 = xgb.XGBClassifier(**cls_params)
    cl2.fit(X, y)
    predt1 = cl2.predict(X)
    proba1: np.ndarray = cl2.predict_proba(X)
    pred_contrib1: np.ndarray = pred_contribs(cl2, X, None, True)

    # convert np array to pyspark dataframe
    cls_df_train_data = [
        (Vectors.dense(X[0, :]), int(y[0])),
        (Vectors.sparse(3, {1: float(X[1, 1]), 2: float(X[1, 2])}), int(y[1])),
    ]
    cls_df_train = spark.createDataFrame(cls_df_train_data, ["features", "label"])

    cls_df_train_large = spark.createDataFrame(
        cls_df_train_data * 100, ["features", "label"]
    )

    cls_df_test = spark.createDataFrame(
        [
            (
                Vectors.dense(X[0, :]),
                int(predt0[0]),
                proba0[0, :].tolist(),
                pred_contrib0[0, :].tolist(),
                int(predt1[0]),
                proba1[0, :].tolist(),
                pred_contrib1[0, :].tolist(),
            ),
            (
                Vectors.sparse(3, {1: 1.0, 2: 5.5}),
                int(predt0[1]),
                proba0[1, :].tolist(),
                pred_contrib0[1, :].tolist(),
                int(predt1[1]),
                proba1[1, :].tolist(),
                pred_contrib1[1, :].tolist(),
            ),
        ],
        [
            "features",
            "expected_prediction",
            "expected_probability",
            "expected_pred_contribs",
            "expected_prediction_with_params",
            "expected_probability_with_params",
            "expected_pred_contribs_with_params",
        ],
    )
    yield ClfData(cls_params, cls_df_train, cls_df_train_large, cls_df_test)


def assert_model_compatible(model: XGBModel, model_path: str) -> None:
    bst = xgb.Booster()
    path = glob.glob(f"{model_path}/**/model/part-00000", recursive=True)[0]
    bst.load_model(path)
    np.testing.assert_equal(
        np.array(model.get_booster().save_raw("json")), np.array(bst.save_raw("json"))
    )


def check_sub_dict_match(
    sub_dist: dict, whole_dict: dict, excluding_keys: Sequence[str]
) -> None:
    for k in sub_dist:
        if k not in excluding_keys:
            assert k in whole_dict, f"check on {k} failed"
            assert sub_dist[k] == whole_dict[k], f"check on {k} failed"


def get_params_map(
    params_kv: dict, estimator: xgb.spark.core._SparkXGBEstimator
) -> dict:
    return {getattr(estimator, k): v for k, v in params_kv.items()}


class TestPySparkLocal:
    def test_regressor_basic(self, reg_data: RegData) -> None:
        regressor = SparkXGBRegressor(pred_contrib_col="pred_contribs")
        model = regressor.fit(reg_data.reg_df_train)
        assert regressor.uid == model.uid
        pred_result = model.transform(reg_data.reg_df_test).collect()
        for row in pred_result:
            np.testing.assert_equal(row.prediction, row.expected_prediction)
            np.testing.assert_allclose(
                row.pred_contribs, row.expected_pred_contribs, atol=1e-3
            )

    def test_regressor_with_weight_eval(self, reg_with_weight: RegWithWeight) -> None:
        # with weight
        regressor_with_weight = SparkXGBRegressor(weight_col="weight")
        model_with_weight = regressor_with_weight.fit(
            reg_with_weight.reg_df_train_with_eval_weight
        )
        pred_result_with_weight = model_with_weight.transform(
            reg_with_weight.reg_df_test_with_eval_weight
        ).collect()
        for row in pred_result_with_weight:
            assert np.isclose(
                row.prediction, row.expected_prediction_with_weight, atol=1e-3
            )

        # with eval
        regressor_with_eval = SparkXGBRegressor(**reg_with_weight.reg_params_with_eval)
        model_with_eval = regressor_with_eval.fit(
            reg_with_weight.reg_df_train_with_eval_weight
        )
        assert np.isclose(
            model_with_eval._xgb_sklearn_model.best_score,
            reg_with_weight.reg_with_eval_best_score,
            atol=1e-3,
        )

        pred_result_with_eval = model_with_eval.transform(
            reg_with_weight.reg_df_test_with_eval_weight
        ).collect()
        for row in pred_result_with_eval:
            np.testing.assert_allclose(
                row.prediction, row.expected_prediction_with_eval, atol=1e-3
            )
        # with weight and eval
        regressor_with_weight_eval = SparkXGBRegressor(
            weight_col="weight", **reg_with_weight.reg_params_with_eval
        )
        model_with_weight_eval = regressor_with_weight_eval.fit(
            reg_with_weight.reg_df_train_with_eval_weight
        )
        pred_result_with_weight_eval = model_with_weight_eval.transform(
            reg_with_weight.reg_df_test_with_eval_weight
        ).collect()
        np.testing.assert_allclose(
            model_with_weight_eval._xgb_sklearn_model.best_score,
            reg_with_weight.reg_with_eval_and_weight_best_score,
            atol=1e-3,
        )
        for row in pred_result_with_weight_eval:
            np.testing.assert_allclose(
                row.prediction,
                row.expected_prediction_with_weight_and_eval,
                atol=1e-3,
            )

    def test_multi_classifier_basic(self, multi_clf_data: MultiClfData) -> None:
        cls = SparkXGBClassifier(pred_contrib_col="pred_contribs")
        model = cls.fit(multi_clf_data.multi_clf_df_train)
        pred_result = model.transform(multi_clf_data.multi_clf_df_test).collect()
        for row in pred_result:
            np.testing.assert_equal(row.prediction, row.expected_prediction)
            np.testing.assert_allclose(
                row.probability, row.expected_probability, rtol=1e-3
            )
            np.testing.assert_allclose(
                row.pred_contribs, row.expected_pred_contribs, atol=1e-3
            )

    def test_classifier_with_weight_eval(self, clf_with_weight: ClfWithWeight) -> None:
        # with weight
        classifier_with_weight = SparkXGBClassifier(weight_col="weight")
        model_with_weight = classifier_with_weight.fit(
            clf_with_weight.cls_df_train_with_eval_weight
        )
        pred_result_with_weight = model_with_weight.transform(
            clf_with_weight.cls_df_test_with_eval_weight
        ).collect()
        for row in pred_result_with_weight:
            assert np.allclose(
                row.probability, row.expected_prob_with_weight, atol=1e-3
            )
        # with eval
        classifier_with_eval = SparkXGBClassifier(
            **clf_with_weight.cls_params_with_eval
        )
        model_with_eval = classifier_with_eval.fit(
            clf_with_weight.cls_df_train_with_eval_weight
        )
        assert np.isclose(
            model_with_eval._xgb_sklearn_model.best_score,
            clf_with_weight.cls_with_eval_best_score,
            atol=1e-3,
        )
        pred_result_with_eval = model_with_eval.transform(
            clf_with_weight.cls_df_test_with_eval_weight
        ).collect()
        for row in pred_result_with_eval:
            assert np.allclose(row.probability, row.expected_prob_with_eval, atol=1e-3)
        # with weight and eval
        classifier_with_weight_eval = SparkXGBClassifier(
            weight_col="weight", **clf_with_weight.cls_params_with_eval
        )
        model_with_weight_eval = classifier_with_weight_eval.fit(
            clf_with_weight.cls_df_train_with_eval_weight
        )
        pred_result_with_weight_eval = model_with_weight_eval.transform(
            clf_with_weight.cls_df_test_with_eval_weight
        ).collect()
        np.testing.assert_allclose(
            model_with_weight_eval._xgb_sklearn_model.best_score,
            clf_with_weight.cls_with_eval_and_weight_best_score,
            atol=1e-3,
        )

        for row in pred_result_with_weight_eval:
            np.testing.assert_allclose(
                row.probability, row.expected_prob_with_weight_and_eval, atol=1e-3
            )

    def test_classifier_model_save_load(self, clf_data: ClfData) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = "file:" + tmpdir
            clf = SparkXGBClassifier(**clf_data.cls_params)
            model = clf.fit(clf_data.cls_df_train)
            model.save(path)
            loaded_model = SparkXGBClassifierModel.load(path)
            assert model.uid == loaded_model.uid
            for k, v in clf_data.cls_params.items():
                assert loaded_model.getOrDefault(k) == v

            pred_result = loaded_model.transform(clf_data.cls_df_test).collect()
            for row in pred_result:
                np.testing.assert_allclose(
                    row.probability, row.expected_probability_with_params, atol=1e-3
                )

            with pytest.raises(AssertionError, match="Expected class name"):
                SparkXGBRegressorModel.load(path)

            assert_model_compatible(model, tmpdir)

    def test_classifier_basic(self, clf_data: ClfData) -> None:
        classifier = SparkXGBClassifier(
            **clf_data.cls_params, pred_contrib_col="pred_contrib"
        )
        model = classifier.fit(clf_data.cls_df_train)
        pred_result = model.transform(clf_data.cls_df_test).collect()
        for row in pred_result:
            np.testing.assert_equal(row.prediction, row.expected_prediction_with_params)
            np.testing.assert_allclose(
                row.probability, row.expected_probability_with_params, rtol=1e-3
            )
            np.testing.assert_equal(
                row.pred_contrib, row.expected_pred_contribs_with_params
            )

    def test_classifier_with_params(self, clf_data: ClfData) -> None:
        classifier = SparkXGBClassifier(**clf_data.cls_params)
        all_params = dict(
            **(classifier._gen_xgb_params_dict()),
            **(classifier._gen_fit_params_dict()),
            **(classifier._gen_predict_params_dict()),
        )
        check_sub_dict_match(
            clf_data.cls_params, all_params, excluding_keys=_non_booster_params
        )

        model = classifier.fit(clf_data.cls_df_train)
        all_params = dict(
            **(model._gen_xgb_params_dict()),
            **(model._gen_fit_params_dict()),
            **(model._gen_predict_params_dict()),
        )
        check_sub_dict_match(
            clf_data.cls_params, all_params, excluding_keys=_non_booster_params
        )
        pred_result = model.transform(clf_data.cls_df_test).collect()
        for row in pred_result:
            np.testing.assert_equal(row.prediction, row.expected_prediction_with_params)
            np.testing.assert_allclose(
                row.probability, row.expected_probability_with_params, rtol=1e-3
            )

    def test_classifier_model_pipeline_save_load(self, clf_data: ClfData) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = "file:" + tmpdir
            classifier = SparkXGBClassifier()
            pipeline = Pipeline(stages=[classifier])
            pipeline = pipeline.copy(
                extra=get_params_map(clf_data.cls_params, classifier)
            )
            model = pipeline.fit(clf_data.cls_df_train)
            model.save(path)

            loaded_model = PipelineModel.load(path)
            for k, v in clf_data.cls_params.items():
                assert loaded_model.stages[0].getOrDefault(k) == v

            pred_result = loaded_model.transform(clf_data.cls_df_test).collect()
            for row in pred_result:
                np.testing.assert_allclose(
                    row.probability, row.expected_probability_with_params, atol=1e-3
                )
            assert_model_compatible(model.stages[0], tmpdir)

    def test_classifier_with_cross_validator(self, clf_data: ClfData) -> None:
        xgb_classifier = SparkXGBClassifier(n_estimators=1)
        paramMaps = ParamGridBuilder().addGrid(xgb_classifier.max_depth, [1, 2]).build()
        cvBin = CrossValidator(
            estimator=xgb_classifier,
            estimatorParamMaps=paramMaps,
            evaluator=BinaryClassificationEvaluator(),
            seed=1,
            parallelism=4,
            numFolds=2,
        )
        cvBinModel = cvBin.fit(clf_data.cls_df_train_large)
        cvBinModel.transform(clf_data.cls_df_test)

    def test_convert_to_sklearn_model_clf(self, clf_data: ClfData) -> None:
        classifier = SparkXGBClassifier(
            n_estimators=200, missing=2.0, max_depth=3, sketch_eps=0.5
        )
        clf_model = classifier.fit(clf_data.cls_df_train)

        # Check that regardless of what booster, _convert_to_model converts to the
        # correct class type
        sklearn_classifier = classifier._convert_to_sklearn_model(
            clf_model.get_booster().save_raw("json"),
            clf_model.get_booster().save_config(),
        )
        assert isinstance(sklearn_classifier, XGBClassifier)
        assert sklearn_classifier.n_estimators == 200
        assert sklearn_classifier.missing == 2.0
        assert sklearn_classifier.max_depth == 3
        assert sklearn_classifier.get_params()["sketch_eps"] == 0.5

    def test_classifier_array_col_as_feature(self, clf_data: ClfData) -> None:
        train_dataset = clf_data.cls_df_train.withColumn(
            "features", vector_to_array(spark_sql_func.col("features"))
        )
        test_dataset = clf_data.cls_df_test.withColumn(
            "features", vector_to_array(spark_sql_func.col("features"))
        )
        classifier = SparkXGBClassifier()
        model = classifier.fit(train_dataset)

        pred_result = model.transform(test_dataset).collect()
        for row in pred_result:
            np.testing.assert_equal(row.prediction, row.expected_prediction)
            np.testing.assert_allclose(
                row.probability, row.expected_probability, rtol=1e-3
            )

    def test_classifier_with_feature_names_types_weights(
        self, clf_data: ClfData
    ) -> None:
        classifier = SparkXGBClassifier(
            feature_names=["a1", "a2", "a3"],
            feature_types=["i", "int", "float"],
            feature_weights=[2.0, 5.0, 3.0],
        )
        model = classifier.fit(clf_data.cls_df_train)
        model.transform(clf_data.cls_df_test).collect()

    def test_early_stop_param_validation(self, clf_data: ClfData) -> None:
        classifier = SparkXGBClassifier(early_stopping_rounds=1)
        with pytest.raises(ValueError, match="early_stopping_rounds"):
            classifier.fit(clf_data.cls_df_train)

    def test_classifier_with_list_eval_metric(self, clf_data: ClfData) -> None:
        classifier = SparkXGBClassifier(eval_metric=["auc", "rmse"])
        model = classifier.fit(clf_data.cls_df_train)
        model.transform(clf_data.cls_df_test).collect()

    def test_classifier_with_string_eval_metric(self, clf_data: ClfData) -> None:
        classifier = SparkXGBClassifier(eval_metric="auc")
        model = classifier.fit(clf_data.cls_df_train)
        model.transform(clf_data.cls_df_test).collect()

    def test_regressor_params_basic(self) -> None:
        py_reg = SparkXGBRegressor()
        assert hasattr(py_reg, "n_estimators")
        assert py_reg.n_estimators.parent == py_reg.uid
        assert not hasattr(py_reg, "gpu_id")
        assert hasattr(py_reg, "device")
        assert py_reg.getOrDefault(py_reg.n_estimators) == 100
        assert py_reg.getOrDefault(getattr(py_reg, "objective")), "reg:squarederror"
        py_reg2 = SparkXGBRegressor(n_estimators=200)
        assert py_reg2.getOrDefault(getattr(py_reg2, "n_estimators")), 200
        py_reg3 = py_reg2.copy({getattr(py_reg2, "max_depth"): 10})
        assert py_reg3.getOrDefault(getattr(py_reg3, "n_estimators")), 200
        assert py_reg3.getOrDefault(getattr(py_reg3, "max_depth")), 10

    def test_classifier_params_basic(self) -> None:
        py_clf = SparkXGBClassifier()
        assert hasattr(py_clf, "n_estimators")
        assert py_clf.n_estimators.parent == py_clf.uid
        assert not hasattr(py_clf, "gpu_id")
        assert hasattr(py_clf, "device")

        assert py_clf.getOrDefault(py_clf.n_estimators) == 100
        assert py_clf.getOrDefault(getattr(py_clf, "objective")) is None
        py_clf2 = SparkXGBClassifier(n_estimators=200)
        assert py_clf2.getOrDefault(getattr(py_clf2, "n_estimators")) == 200
        py_clf3 = py_clf2.copy({getattr(py_clf2, "max_depth"): 10})
        assert py_clf3.getOrDefault(getattr(py_clf3, "n_estimators")) == 200
        assert py_clf3.getOrDefault(getattr(py_clf3, "max_depth")), 10

    def test_classifier_kwargs_basic(self, clf_data: ClfData) -> None:
        py_clf = SparkXGBClassifier(**clf_data.cls_params)
        assert hasattr(py_clf, "n_estimators")
        assert py_clf.n_estimators.parent == py_clf.uid
        assert not hasattr(py_clf, "gpu_id")
        assert hasattr(py_clf, "device")
        assert hasattr(py_clf, "arbitrary_params_dict")
        assert py_clf.getOrDefault(py_clf.arbitrary_params_dict) == {}

        # Testing overwritten params
        py_clf = SparkXGBClassifier()
        py_clf.setParams(x=1, y=2)
        py_clf.setParams(y=3, z=4)
        xgb_params = py_clf._gen_xgb_params_dict()
        assert xgb_params["x"] == 1
        assert xgb_params["y"] == 3
        assert xgb_params["z"] == 4

    def test_regressor_model_save_load(self, reg_data: RegData) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = "file:" + tmpdir
            regressor = SparkXGBRegressor(**reg_data.reg_params)
            model = regressor.fit(reg_data.reg_df_train)
            model.save(path)
            loaded_model = SparkXGBRegressorModel.load(path)
            assert model.uid == loaded_model.uid
            for k, v in reg_data.reg_params.items():
                assert loaded_model.getOrDefault(k) == v

            pred_result = loaded_model.transform(reg_data.reg_df_test).collect()
            for row in pred_result:
                assert np.isclose(
                    row.prediction, row.expected_prediction_with_params, atol=1e-3
                )

            with pytest.raises(AssertionError, match="Expected class name"):
                SparkXGBClassifierModel.load(path)

            assert_model_compatible(model, tmpdir)

    def test_regressor_with_params(self, reg_data: RegData) -> None:
        regressor = SparkXGBRegressor(**reg_data.reg_params)
        all_params = dict(
            **(regressor._gen_xgb_params_dict()),
            **(regressor._gen_fit_params_dict()),
            **(regressor._gen_predict_params_dict()),
        )
        check_sub_dict_match(
            reg_data.reg_params, all_params, excluding_keys=_non_booster_params
        )

        model = regressor.fit(reg_data.reg_df_train)
        all_params = dict(
            **(model._gen_xgb_params_dict()),
            **(model._gen_fit_params_dict()),
            **(model._gen_predict_params_dict()),
        )
        check_sub_dict_match(
            reg_data.reg_params, all_params, excluding_keys=_non_booster_params
        )
        pred_result = model.transform(reg_data.reg_df_test).collect()
        for row in pred_result:
            assert np.isclose(
                row.prediction, row.expected_prediction_with_params, atol=1e-3
            )

    def test_regressor_model_pipeline_save_load(self, reg_data: RegData) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = "file:" + tmpdir
            regressor = SparkXGBRegressor()
            pipeline = Pipeline(stages=[regressor])
            pipeline = pipeline.copy(
                extra=get_params_map(reg_data.reg_params, regressor)
            )
            model = pipeline.fit(reg_data.reg_df_train)
            model.save(path)

            loaded_model = PipelineModel.load(path)
            for k, v in reg_data.reg_params.items():
                assert loaded_model.stages[0].getOrDefault(k) == v

            pred_result = loaded_model.transform(reg_data.reg_df_test).collect()
            for row in pred_result:
                assert np.isclose(
                    row.prediction, row.expected_prediction_with_params, atol=1e-3
                )
            assert_model_compatible(model.stages[0], tmpdir)

    def test_with_small_model_chunk_size(self, reg_data: RegData, monkeypatch) -> None:
        import xgboost.spark.core

        monkeypatch.setattr(xgboost.spark.core, "_MODEL_CHUNK_SIZE", 4)
        with tempfile.TemporaryDirectory() as tmpdir:
            path = "file:" + tmpdir
            regressor = SparkXGBRegressor(**reg_data.reg_params)
            model = regressor.fit(reg_data.reg_df_train)
            model.save(path)
            loaded_model = SparkXGBRegressorModel.load(path)
            assert model.uid == loaded_model.uid
            for k, v in reg_data.reg_params.items():
                assert loaded_model.getOrDefault(k) == v

            pred_result = loaded_model.transform(reg_data.reg_df_test).collect()
            for row in pred_result:
                assert np.isclose(
                    row.prediction, row.expected_prediction_with_params, atol=1e-3
                )

    def test_device_param(self, reg_data: RegData, clf_data: ClfData) -> None:
        clf = SparkXGBClassifier(device="cuda", tree_method="exact")
        with pytest.raises(ValueError, match="not supported for distributed"):
            clf.fit(clf_data.cls_df_train)
        regressor = SparkXGBRegressor(device="cuda", tree_method="exact")
        with pytest.raises(ValueError, match="not supported for distributed"):
            regressor.fit(reg_data.reg_df_train)

        reg = SparkXGBRegressor(device="cuda", tree_method="hist")
        reg._validate_params()
        reg = SparkXGBRegressor(device="cuda")
        reg._validate_params()

        clf = SparkXGBClassifier(device="cuda", tree_method="approx")
        clf._validate_params()
        clf = SparkXGBClassifier(device="cuda")
        clf._validate_params()

    def test_gpu_params(self) -> None:
        clf = SparkXGBClassifier()
        assert not clf._run_on_gpu()

        clf = SparkXGBClassifier(device="cuda", tree_method="hist")
        assert clf._run_on_gpu()

        clf = SparkXGBClassifier(device="cuda")
        assert clf._run_on_gpu()

        clf = SparkXGBClassifier(tree_method="hist")
        assert not clf._run_on_gpu()

        clf = SparkXGBClassifier(device="cuda", tree_method="approx")
        assert clf._run_on_gpu()

    def test_gpu_transform(self, clf_data: ClfData) -> None:
        """local mode"""
        classifier = SparkXGBClassifier(device="cpu")
        model: SparkXGBClassifierModel = classifier.fit(clf_data.cls_df_train)

        with tempfile.TemporaryDirectory() as tmpdir:
            path = "file:" + tmpdir
            model.write().overwrite().save(path)

            # The model trained with CPU, transform defaults to cpu
            assert not model._run_on_gpu()

            # without error
            model.transform(clf_data.cls_df_test).collect()

            model.set_device("cuda")
            assert model._run_on_gpu()

            model_loaded = SparkXGBClassifierModel.load(path)

            # The model trained with CPU, transform defaults to cpu
            assert not model_loaded._run_on_gpu()
            # without error
            model_loaded.transform(clf_data.cls_df_test).collect()

            model_loaded.set_device("cuda")
            assert model_loaded._run_on_gpu()

    def test_validate_gpu_params(self) -> None:
        # Standalone
        standalone_conf = (
            SparkConf()
            .setMaster("spark://foo")
            .set("spark.executor.cores", "12")
            .set("spark.task.cpus", "1")
            .set("spark.executor.resource.gpu.amount", "1")
            .set("spark.task.resource.gpu.amount", "0.08")
        )
        classifier_on_cpu = SparkXGBClassifier(device="cpu")
        classifier_on_gpu = SparkXGBClassifier(device="cuda")

        # No exception for classifier on CPU
        classifier_on_cpu._validate_gpu_params("3.4.0", standalone_conf)

        with pytest.raises(
            ValueError, match="XGBoost doesn't support GPU fractional configurations"
        ):
            classifier_on_gpu._validate_gpu_params("3.3.0", standalone_conf)

        # No issues
        classifier_on_gpu._validate_gpu_params("3.4.0", standalone_conf)
        classifier_on_gpu._validate_gpu_params("3.4.1", standalone_conf)
        classifier_on_gpu._validate_gpu_params("3.5.0", standalone_conf)
        classifier_on_gpu._validate_gpu_params("3.5.1", standalone_conf)

        # no spark.executor.resource.gpu.amount
        standalone_bad_conf = (
            SparkConf()
            .setMaster("spark://foo")
            .set("spark.executor.cores", "12")
            .set("spark.task.cpus", "1")
            .set("spark.task.resource.gpu.amount", "0.08")
        )
        msg_match = (
            "The `spark.executor.resource.gpu.amount` is required for training on GPU"
        )
        with pytest.raises(ValueError, match=msg_match):
            classifier_on_gpu._validate_gpu_params("3.3.0", standalone_bad_conf)
        with pytest.raises(ValueError, match=msg_match):
            classifier_on_gpu._validate_gpu_params("3.4.0", standalone_bad_conf)
        with pytest.raises(ValueError, match=msg_match):
            classifier_on_gpu._validate_gpu_params("3.4.1", standalone_bad_conf)
        with pytest.raises(ValueError, match=msg_match):
            classifier_on_gpu._validate_gpu_params("3.5.0", standalone_bad_conf)
        with pytest.raises(ValueError, match=msg_match):
            classifier_on_gpu._validate_gpu_params("3.5.1", standalone_bad_conf)

        standalone_bad_conf = (
            SparkConf()
            .setMaster("spark://foo")
            .set("spark.executor.cores", "12")
            .set("spark.task.cpus", "1")
            .set("spark.executor.resource.gpu.amount", "1")
        )
        msg_match = (
            "The `spark.task.resource.gpu.amount` is required for training on GPU"
        )
        with pytest.raises(ValueError, match=msg_match):
            classifier_on_gpu._validate_gpu_params("3.3.0", standalone_bad_conf)

        classifier_on_gpu._validate_gpu_params("3.4.0", standalone_bad_conf)
        classifier_on_gpu._validate_gpu_params("3.5.0", standalone_bad_conf)
        classifier_on_gpu._validate_gpu_params("3.5.1", standalone_bad_conf)

        # Yarn and K8s mode
        for mode in ["yarn", "k8s://"]:
            conf = (
                SparkConf()
                .setMaster(mode)
                .set("spark.executor.cores", "12")
                .set("spark.task.cpus", "1")
                .set("spark.executor.resource.gpu.amount", "1")
                .set("spark.task.resource.gpu.amount", "0.08")
            )
            with pytest.raises(
                ValueError,
                match="XGBoost doesn't support GPU fractional configurations",
            ):
                classifier_on_gpu._validate_gpu_params("3.3.0", conf)
            with pytest.raises(
                ValueError,
                match="XGBoost doesn't support GPU fractional configurations",
            ):
                classifier_on_gpu._validate_gpu_params("3.4.0", conf)
            with pytest.raises(
                ValueError,
                match="XGBoost doesn't support GPU fractional configurations",
            ):
                classifier_on_gpu._validate_gpu_params("3.4.1", conf)
            with pytest.raises(
                ValueError,
                match="XGBoost doesn't support GPU fractional configurations",
            ):
                classifier_on_gpu._validate_gpu_params("3.5.0", conf)

            classifier_on_gpu._validate_gpu_params("3.5.1", conf)

        for mode in ["yarn", "k8s://"]:
            bad_conf = (
                SparkConf()
                .setMaster(mode)
                .set("spark.executor.cores", "12")
                .set("spark.task.cpus", "1")
                .set("spark.executor.resource.gpu.amount", "1")
            )
            msg_match = (
                "The `spark.task.resource.gpu.amount` is required for training on GPU"
            )
            with pytest.raises(ValueError, match=msg_match):
                classifier_on_gpu._validate_gpu_params("3.3.0", bad_conf)
            with pytest.raises(ValueError, match=msg_match):
                classifier_on_gpu._validate_gpu_params("3.4.0", bad_conf)
            with pytest.raises(ValueError, match=msg_match):
                classifier_on_gpu._validate_gpu_params("3.5.0", bad_conf)

            classifier_on_gpu._validate_gpu_params("3.5.1", bad_conf)

    def test_skip_stage_level_scheduling(self) -> None:
        standalone_conf = (
            SparkConf()
            .setMaster("spark://foo")
            .set("spark.executor.cores", "12")
            .set("spark.task.cpus", "1")
            .set("spark.executor.resource.gpu.amount", "1")
            .set("spark.task.resource.gpu.amount", "0.08")
        )

        classifier_on_cpu = SparkXGBClassifier(device="cpu")
        classifier_on_gpu = SparkXGBClassifier(device="cuda")

        # the correct configurations should not skip stage-level scheduling
        assert not classifier_on_gpu._skip_stage_level_scheduling(
            "3.4.0", standalone_conf
        )
        assert not classifier_on_gpu._skip_stage_level_scheduling(
            "3.4.1", standalone_conf
        )
        assert not classifier_on_gpu._skip_stage_level_scheduling(
            "3.5.0", standalone_conf
        )
        assert not classifier_on_gpu._skip_stage_level_scheduling(
            "3.5.1", standalone_conf
        )

        # spark version < 3.4.0
        assert classifier_on_gpu._skip_stage_level_scheduling("3.3.0", standalone_conf)
        # not run on GPU
        assert classifier_on_cpu._skip_stage_level_scheduling("3.4.0", standalone_conf)

        # spark.executor.cores is not set
        bad_conf = (
            SparkConf()
            .setMaster("spark://foo")
            .set("spark.task.cpus", "1")
            .set("spark.executor.resource.gpu.amount", "1")
            .set("spark.task.resource.gpu.amount", "0.08")
        )
        assert classifier_on_gpu._skip_stage_level_scheduling("3.4.0", bad_conf)

        # spark.executor.cores=1
        bad_conf = (
            SparkConf()
            .setMaster("spark://foo")
            .set("spark.executor.cores", "1")
            .set("spark.task.cpus", "1")
            .set("spark.executor.resource.gpu.amount", "1")
            .set("spark.task.resource.gpu.amount", "0.08")
        )
        assert classifier_on_gpu._skip_stage_level_scheduling("3.4.0", bad_conf)

        # spark.executor.resource.gpu.amount is not set
        bad_conf = (
            SparkConf()
            .setMaster("spark://foo")
            .set("spark.executor.cores", "12")
            .set("spark.task.cpus", "1")
            .set("spark.task.resource.gpu.amount", "0.08")
        )
        assert classifier_on_gpu._skip_stage_level_scheduling("3.4.0", bad_conf)

        # spark.executor.resource.gpu.amount>1
        bad_conf = (
            SparkConf()
            .setMaster("spark://foo")
            .set("spark.executor.cores", "12")
            .set("spark.task.cpus", "1")
            .set("spark.executor.resource.gpu.amount", "2")
            .set("spark.task.resource.gpu.amount", "0.08")
        )
        assert classifier_on_gpu._skip_stage_level_scheduling("3.4.0", bad_conf)

        # spark.task.resource.gpu.amount is not set
        bad_conf = (
            SparkConf()
            .setMaster("spark://foo")
            .set("spark.executor.cores", "12")
            .set("spark.task.cpus", "1")
            .set("spark.executor.resource.gpu.amount", "1")
        )
        assert not classifier_on_gpu._skip_stage_level_scheduling("3.4.0", bad_conf)

        # spark.task.resource.gpu.amount=1
        bad_conf = (
            SparkConf()
            .setMaster("spark://foo")
            .set("spark.executor.cores", "12")
            .set("spark.task.cpus", "1")
            .set("spark.executor.resource.gpu.amount", "1")
            .set("spark.task.resource.gpu.amount", "1")
        )
        assert classifier_on_gpu._skip_stage_level_scheduling("3.4.0", bad_conf)

        # For Yarn and K8S
        for mode in ["yarn", "k8s://"]:
            for gpu_amount in ["0.08", "0.2", "1.0"]:
                conf = (
                    SparkConf()
                    .setMaster(mode)
                    .set("spark.executor.cores", "12")
                    .set("spark.task.cpus", "1")
                    .set("spark.executor.resource.gpu.amount", "1")
                    .set("spark.task.resource.gpu.amount", gpu_amount)
                )
                assert classifier_on_gpu._skip_stage_level_scheduling("3.3.0", conf)
                assert classifier_on_gpu._skip_stage_level_scheduling("3.4.0", conf)
                assert classifier_on_gpu._skip_stage_level_scheduling("3.4.1", conf)
                assert classifier_on_gpu._skip_stage_level_scheduling("3.5.0", conf)

                # This will be fixed when spark 4.0.0 is released.
                if gpu_amount == "1.0":
                    assert classifier_on_gpu._skip_stage_level_scheduling("3.5.1", conf)
                else:
                    # Starting from 3.5.1+, stage-level scheduling is working for Yarn and K8s
                    assert not classifier_on_gpu._skip_stage_level_scheduling(
                        "3.5.1", conf
                    )

    @pytest.mark.parametrize("tree_method", ["hist", "approx"])
    def test_empty_train_data(self, spark: SparkSession, tree_method: str) -> None:
        df_train = spark.createDataFrame(
            [
                (Vectors.dense(10.1, 11.2, 11.3), 0, True),
                (Vectors.dense(1, 1.2, 1.3), 1, True),
                (Vectors.dense(14.0, 15.0, 16.0), 0, True),
                (Vectors.dense(1.1, 1.2, 1.3), 1, False),
            ],
            ["features", "label", "val_col"],
        )
        classifier = SparkXGBRegressor(
            num_workers=2,
            min_child_weight=0.0,
            reg_alpha=0,
            reg_lambda=0,
            tree_method=tree_method,
            validation_indicator_col="val_col",
        )
        model = classifier.fit(df_train)
        pred_result = model.transform(df_train).collect()
        for row in pred_result:
            assert row.prediction == 1.0

    def test_regressor_xgb_summary(self, reg_with_weight: RegWithWeight) -> None:
        reg_df_train = reg_with_weight.reg_df_train_with_eval_weight.filter(
            spark_sql_func.col("isVal") == False
        )
        spark_xgb_model = SparkXGBRegressor(eval_metric="rmse").fit(reg_df_train)

        np.testing.assert_allclose(
            reg_with_weight.reg_expected_evals_result_train,
            spark_xgb_model.training_summary.train_objective_history["rmse"],
            atol=1e-3,
        )

        assert spark_xgb_model.training_summary.validation_objective_history == {}

    def test_regressor_xgb_summary_with_validation(
        self, reg_with_weight: RegWithWeight
    ) -> None:
        spark_xgb_model = SparkXGBRegressor(
            eval_metric="rmse", validation_indicator_col="isVal"
        ).fit(
            reg_with_weight.reg_df_train_with_eval_weight,
        )

        np.testing.assert_allclose(
            reg_with_weight.reg_expected_evals_result_train,
            spark_xgb_model.training_summary.train_objective_history["rmse"],
            atol=1e-3,
        )

        np.testing.assert_allclose(
            reg_with_weight.reg_expected_evals_result_validation,
            spark_xgb_model.training_summary.validation_objective_history["rmse"],
            atol=1e-3,
        )

    def test_classifier_xgb_summary(self, clf_with_weight: ClfWithWeight) -> None:
        clf_df_train = clf_with_weight.cls_df_train_with_eval_weight.filter(
            spark_sql_func.col("isVal") == False
        )
        spark_xgb_model = SparkXGBClassifier(eval_metric="logloss").fit(clf_df_train)

        np.testing.assert_allclose(
            clf_with_weight.cls_expected_evals_result_train,
            spark_xgb_model.training_summary.train_objective_history["logloss"],
            atol=1e-3,
        )

        assert spark_xgb_model.training_summary.validation_objective_history == {}

    def test_classifier_xgb_summary_with_validation(
        self, clf_with_weight: ClfWithWeight
    ) -> None:
        spark_xgb_model = SparkXGBClassifier(
            eval_metric="logloss", validation_indicator_col="isVal"
        ).fit(
            clf_with_weight.cls_df_train_with_eval_weight,
        )

        np.testing.assert_allclose(
            clf_with_weight.cls_expected_evals_result_train,
            spark_xgb_model.training_summary.train_objective_history["logloss"],
            atol=1e-3,
        )

        np.testing.assert_allclose(
            clf_with_weight.cls_expected_evals_result_validation,
            spark_xgb_model.training_summary.validation_objective_history["logloss"],
            atol=1e-3,
        )

    def test_valid_type(self, spark: SparkSession) -> None:
        # Validation indicator must be boolean.
        df_train = spark.createDataFrame(
            [
                (Vectors.dense(1.0, 2.0, 3.0), 0, 0),
                (Vectors.sparse(3, {1: 1.0, 2: 5.5}), 1, 0),
                (Vectors.dense(4.0, 5.0, 6.0), 0, 1),
                (Vectors.sparse(3, {1: 6.0, 2: 7.5}), 1, 1),
            ],
            ["features", "label", "isVal"],
        )
        reg = SparkXGBRegressor(
            features_col="features",
            label_col="label",
            validation_indicator_col="isVal",
        )
        with pytest.raises(TypeError, match="The validation indicator must be boolean"):
            reg.fit(df_train)


class XgboostLocalTest(SparkTestCase):
    def setUp(self):
        logging.getLogger().setLevel("INFO")
        random.seed(2020)

        # The following code use xgboost python library to train xgb model and predict.
        #
        # >>> import numpy as np
        # >>> import xgboost
        # >>> X = np.array([[1.0, 2.0, 3.0], [0.0, 1.0, 5.5]])
        # >>> y = np.array([0, 1])
        # >>> reg1 = xgboost.XGBRegressor()
        # >>> reg1.fit(X, y)
        # >>> reg1.predict(X)
        # array([8.8375784e-04, 9.9911624e-01], dtype=float32)
        # >>> def custom_lr(boosting_round):
        # ...     return 1.0 / (boosting_round + 1)
        # ...
        # >>> reg1.fit(X, y, callbacks=[xgboost.callback.LearningRateScheduler(custom_lr)])
        # >>> reg1.predict(X)
        # array([0.02406844, 0.9759315 ], dtype=float32)
        # >>> reg2 = xgboost.XGBRegressor(max_depth=5, n_estimators=10)
        # >>> reg2.fit(X, y)
        # >>> reg2.predict(X, ntree_limit=5)
        # array([0.22185266, 0.77814734], dtype=float32)
        self.reg_params = {
            "max_depth": 5,
            "n_estimators": 10,
            "ntree_limit": 5,
            "max_bin": 9,
        }
        self.reg_df_train = self.session.createDataFrame(
            [
                (Vectors.dense(1.0, 2.0, 3.0), 0),
                (Vectors.sparse(3, {1: 1.0, 2: 5.5}), 1),
            ],
            ["features", "label"],
        )
        self.reg_df_test = self.session.createDataFrame(
            [
                (Vectors.dense(1.0, 2.0, 3.0), 0.0, 0.2219, 0.02406),
                (Vectors.sparse(3, {1: 1.0, 2: 5.5}), 1.0, 0.7781, 0.9759),
            ],
            [
                "features",
                "expected_prediction",
                "expected_prediction_with_params",
                "expected_prediction_with_callbacks",
            ],
        )

        # kwargs test (using the above data, train, we get the same results)
        self.cls_params_kwargs = {"tree_method": "approx", "sketch_eps": 0.03}

        # >>> X = np.array([[1.0, 2.0, 3.0], [1.0, 2.0, 4.0], [0.0, 1.0, 5.5], [-1.0, -2.0, 1.0]])
        # >>> y = np.array([0, 0, 1, 2])
        # >>> cl = xgboost.XGBClassifier()
        # >>> cl.fit(X, y)
        # >>> cl.predict_proba(np.array([[1.0, 2.0, 3.0]]))
        # array([[0.5374299 , 0.23128504, 0.23128504]], dtype=float32)

        # Test classifier with both base margin and without
        # >>> import numpy as np
        # >>> import xgboost
        # >>> X = np.array([[1.0, 2.0, 3.0], [0.0, 1.0, 5.5], [4.0, 5.0, 6.0], [0.0, 6.0, 7.5]])
        # >>> w = np.array([1.0, 2.0, 1.0, 2.0])
        # >>> y = np.array([0, 1, 0, 1])
        # >>> base_margin = np.array([1,0,0,1])
        #
        # This is without the base margin
        # >>> cls1 = xgboost.XGBClassifier()
        # >>> cls1.fit(X, y, sample_weight=w)
        # >>> cls1.predict_proba(np.array([[1.0, 2.0, 3.0]]))
        # array([[0.3333333, 0.6666667]], dtype=float32)
        # >>> cls1.predict(np.array([[1.0, 2.0, 3.0]]))
        # array([1])
        #
        # This is with the same base margin for predict
        # >>> cls2 = xgboost.XGBClassifier()
        # >>> cls2.fit(X, y, sample_weight=w, base_margin=base_margin)
        # >>> cls2.predict_proba(np.array([[1.0, 2.0, 3.0]]), base_margin=[0])
        # array([[0.44142532, 0.5585747 ]], dtype=float32)
        # >>> cls2.predict(np.array([[1.0, 2.0, 3.0]]), base_margin=[0])
        # array([1])
        #
        # This is with a different base margin for predict
        # # >>> cls2 = xgboost.XGBClassifier()
        # >>> cls2.fit(X, y, sample_weight=w, base_margin=base_margin)
        # >>> cls2.predict_proba(np.array([[1.0, 2.0, 3.0]]), base_margin=[1])
        # array([[0.2252, 0.7747 ]], dtype=float32)
        # >>> cls2.predict(np.array([[1.0, 2.0, 3.0]]), base_margin=[0])
        # array([1])
        self.cls_df_train_without_base_margin = self.session.createDataFrame(
            [
                (Vectors.dense(1.0, 2.0, 3.0), 0, 1.0),
                (Vectors.sparse(3, {1: 1.0, 2: 5.5}), 1, 2.0),
                (Vectors.dense(4.0, 5.0, 6.0), 0, 1.0),
                (Vectors.sparse(3, {1: 6.0, 2: 7.5}), 1, 2.0),
            ],
            ["features", "label", "weight"],
        )
        self.cls_df_test_without_base_margin = self.session.createDataFrame(
            [
                (Vectors.dense(1.0, 2.0, 3.0), [0.3333, 0.6666], 1),
            ],
            [
                "features",
                "expected_prob_without_base_margin",
                "expected_prediction_without_base_margin",
            ],
        )

        self.cls_df_train_with_same_base_margin = self.session.createDataFrame(
            [
                (Vectors.dense(1.0, 2.0, 3.0), 0, 1.0, 1),
                (Vectors.sparse(3, {1: 1.0, 2: 5.5}), 1, 2.0, 0),
                (Vectors.dense(4.0, 5.0, 6.0), 0, 1.0, 0),
                (Vectors.sparse(3, {1: 6.0, 2: 7.5}), 1, 2.0, 1),
            ],
            ["features", "label", "weight", "base_margin"],
        )
        self.cls_df_test_with_same_base_margin = self.session.createDataFrame(
            [
                (Vectors.dense(1.0, 2.0, 3.0), 0, [0.4415, 0.5585], 1),
            ],
            [
                "features",
                "base_margin",
                "expected_prob_with_base_margin",
                "expected_prediction_with_base_margin",
            ],
        )

        self.cls_df_train_with_different_base_margin = self.session.createDataFrame(
            [
                (Vectors.dense(1.0, 2.0, 3.0), 0, 1.0, 1),
                (Vectors.sparse(3, {1: 1.0, 2: 5.5}), 1, 2.0, 0),
                (Vectors.dense(4.0, 5.0, 6.0), 0, 1.0, 0),
                (Vectors.sparse(3, {1: 6.0, 2: 7.5}), 1, 2.0, 1),
            ],
            ["features", "label", "weight", "base_margin"],
        )
        self.cls_df_test_with_different_base_margin = self.session.createDataFrame(
            [
                (Vectors.dense(1.0, 2.0, 3.0), 1, [0.2252, 0.7747], 1),
            ],
            [
                "features",
                "base_margin",
                "expected_prob_with_base_margin",
                "expected_prediction_with_base_margin",
            ],
        )

        self.reg_df_sparse_train = self.session.createDataFrame(
            [
                (Vectors.dense(1.0, 0.0, 3.0, 0.0, 0.0), 0),
                (Vectors.sparse(5, {1: 1.0, 3: 5.5}), 1),
                (Vectors.sparse(5, {4: -3.0}), 2),
            ]
            * 10,
            ["features", "label"],
        )

        self.cls_df_sparse_train = self.session.createDataFrame(
            [
                (Vectors.dense(1.0, 0.0, 3.0, 0.0, 0.0), 0),
                (Vectors.sparse(5, {1: 1.0, 3: 5.5}), 1),
                (Vectors.sparse(5, {4: -3.0}), 0),
            ]
            * 10,
            ["features", "label"],
        )

    def get_local_tmp_dir(self):
        return self.tempdir + str(uuid.uuid4())

    def test_convert_to_sklearn_model_reg(self) -> None:
        regressor = SparkXGBRegressor(
            n_estimators=200, missing=2.0, max_depth=3, sketch_eps=0.5
        )
        reg_model = regressor.fit(self.reg_df_train)

        sklearn_regressor = regressor._convert_to_sklearn_model(
            reg_model.get_booster().save_raw("json"),
            reg_model.get_booster().save_config(),
        )
        assert isinstance(sklearn_regressor, XGBRegressor)
        assert sklearn_regressor.n_estimators == 200
        assert sklearn_regressor.missing == 2.0
        assert sklearn_regressor.max_depth == 3
        assert sklearn_regressor.get_params()["sketch_eps"] == 0.5

    def test_param_alias(self):
        py_cls = SparkXGBClassifier(features_col="f1", label_col="l1")
        self.assertEqual(py_cls.getOrDefault(py_cls.featuresCol), "f1")
        self.assertEqual(py_cls.getOrDefault(py_cls.labelCol), "l1")
        with pytest.raises(
            ValueError, match="Please use param name features_col instead"
        ):
            SparkXGBClassifier(featuresCol="f1")

    @staticmethod
    def test_param_value_converter():
        py_cls = SparkXGBClassifier(missing=np.float64(1.0), sketch_eps=np.float64(0.3))
        # don't check by isintance(v, float) because for numpy scalar it will also return True
        assert py_cls.getOrDefault(py_cls.missing).__class__.__name__ == "float"
        assert (
            py_cls.getOrDefault(py_cls.arbitrary_params_dict)[
                "sketch_eps"
            ].__class__.__name__
            == "float64"
        )

    def test_callbacks(self):
        from xgboost.callback import LearningRateScheduler

        path = self.get_local_tmp_dir()

        def custom_learning_rate(boosting_round):
            return 1.0 / (boosting_round + 1)

        cb = [LearningRateScheduler(custom_learning_rate)]
        regressor = SparkXGBRegressor(callbacks=cb)

        # Test the save/load of the estimator instead of the model, since
        # the callbacks param only exists in the estimator but not in the model
        regressor.save(path)
        regressor = SparkXGBRegressor.load(path)

        model = regressor.fit(self.reg_df_train)
        pred_result = model.transform(self.reg_df_test).collect()
        for row in pred_result:
            self.assertTrue(
                np.isclose(
                    row.prediction, row.expected_prediction_with_callbacks, atol=1e-3
                )
            )

    def test_train_with_initial_model(self):
        path = self.get_local_tmp_dir()
        reg1 = SparkXGBRegressor(**self.reg_params)
        model = reg1.fit(self.reg_df_train)
        init_booster = model.get_booster()
        reg2 = SparkXGBRegressor(
            max_depth=2, n_estimators=2, xgb_model=init_booster, max_bin=21
        )
        model21 = reg2.fit(self.reg_df_train)
        pred_res21 = model21.transform(self.reg_df_test).collect()
        reg2.save(path)
        reg2 = SparkXGBRegressor.load(path)
        self.assertTrue(reg2.getOrDefault(reg2.xgb_model) is not None)
        model22 = reg2.fit(self.reg_df_train)
        pred_res22 = model22.transform(self.reg_df_test).collect()
        # Test the transform result is the same for original and loaded model
        for row1, row2 in zip(pred_res21, pred_res22):
            self.assertTrue(np.isclose(row1.prediction, row2.prediction, atol=1e-3))

    def test_classifier_with_base_margin(self):
        cls_without_base_margin = SparkXGBClassifier(weight_col="weight")
        model_without_base_margin = cls_without_base_margin.fit(
            self.cls_df_train_without_base_margin
        )
        pred_result_without_base_margin = model_without_base_margin.transform(
            self.cls_df_test_without_base_margin
        ).collect()
        for row in pred_result_without_base_margin:
            self.assertTrue(
                np.isclose(
                    row.prediction,
                    row.expected_prediction_without_base_margin,
                    atol=1e-3,
                )
            )
            np.testing.assert_allclose(
                row.probability, row.expected_prob_without_base_margin, atol=1e-3
            )

        cls_with_same_base_margin = SparkXGBClassifier(
            weight_col="weight", base_margin_col="base_margin"
        )
        model_with_same_base_margin = cls_with_same_base_margin.fit(
            self.cls_df_train_with_same_base_margin
        )
        pred_result_with_same_base_margin = model_with_same_base_margin.transform(
            self.cls_df_test_with_same_base_margin
        ).collect()
        for row in pred_result_with_same_base_margin:
            self.assertTrue(
                np.isclose(
                    row.prediction, row.expected_prediction_with_base_margin, atol=1e-3
                )
            )
            np.testing.assert_allclose(
                row.probability, row.expected_prob_with_base_margin, atol=1e-3
            )

        cls_with_different_base_margin = SparkXGBClassifier(
            weight_col="weight", base_margin_col="base_margin"
        )
        model_with_different_base_margin = cls_with_different_base_margin.fit(
            self.cls_df_train_with_different_base_margin
        )
        pred_result_with_different_base_margin = (
            model_with_different_base_margin.transform(
                self.cls_df_test_with_different_base_margin
            ).collect()
        )
        for row in pred_result_with_different_base_margin:
            self.assertTrue(
                np.isclose(
                    row.prediction, row.expected_prediction_with_base_margin, atol=1e-3
                )
            )
            np.testing.assert_allclose(
                row.probability, row.expected_prob_with_base_margin, atol=1e-3
            )

    def test_num_workers_param(self):
        regressor = SparkXGBRegressor(num_workers=-1)
        self.assertRaises(ValueError, regressor._validate_params)
        classifier = SparkXGBClassifier(num_workers=0)
        self.assertRaises(ValueError, classifier._validate_params)

    def test_feature_importances(self):
        reg1 = SparkXGBRegressor(**self.reg_params)
        model = reg1.fit(self.reg_df_train)
        booster = model.get_booster()
        self.assertEqual(model.get_feature_importances(), booster.get_score())
        self.assertEqual(
            model.get_feature_importances(importance_type="gain"),
            booster.get_score(importance_type="gain"),
        )

    def test_regressor_array_col_as_feature(self):
        train_dataset = self.reg_df_train.withColumn(
            "features", vector_to_array(spark_sql_func.col("features"))
        )
        test_dataset = self.reg_df_test.withColumn(
            "features", vector_to_array(spark_sql_func.col("features"))
        )
        regressor = SparkXGBRegressor()
        model = regressor.fit(train_dataset)
        pred_result = model.transform(test_dataset).collect()
        for row in pred_result:
            self.assertTrue(
                np.isclose(row.prediction, row.expected_prediction, atol=1e-3)
            )

    @pytest.mark.skipif(**no_sparse_unwrap())
    def test_regressor_with_sparse_optim(self):
        regressor = SparkXGBRegressor(missing=0.0)
        model = regressor.fit(self.reg_df_sparse_train)
        assert model._xgb_sklearn_model.missing == 0.0
        pred_result = model.transform(self.reg_df_sparse_train).collect()

        # enable sparse optimiaztion
        regressor2 = SparkXGBRegressor(missing=0.0, enable_sparse_data_optim=True)
        model2 = regressor2.fit(self.reg_df_sparse_train)
        assert model2.getOrDefault(model2.enable_sparse_data_optim)
        assert model2._xgb_sklearn_model.missing == 0.0
        pred_result2 = model2.transform(self.reg_df_sparse_train).collect()

        for row1, row2 in zip(pred_result, pred_result2):
            self.assertTrue(np.isclose(row1.prediction, row2.prediction, atol=1e-3))

    @pytest.mark.skipif(**no_sparse_unwrap())
    def test_classifier_with_sparse_optim(self):
        cls = SparkXGBClassifier(missing=0.0)
        model = cls.fit(self.cls_df_sparse_train)
        assert model._xgb_sklearn_model.missing == 0.0
        pred_result = model.transform(self.cls_df_sparse_train).collect()

        # enable sparse optimiaztion
        cls2 = SparkXGBClassifier(missing=0.0, enable_sparse_data_optim=True)
        model2 = cls2.fit(self.cls_df_sparse_train)
        assert model2.getOrDefault(model2.enable_sparse_data_optim)
        assert model2._xgb_sklearn_model.missing == 0.0
        pred_result2 = model2.transform(self.cls_df_sparse_train).collect()

        for row1, row2 in zip(pred_result, pred_result2):
            self.assertTrue(np.allclose(row1.probability, row2.probability, rtol=1e-3))

    def test_empty_validation_data(self) -> None:
        for tree_method in [
            "hist",
            "approx",
        ]:  # pytest.mark conflict with python unittest
            df_train = self.session.createDataFrame(
                [
                    (Vectors.dense(10.1, 11.2, 11.3), 0, False),
                    (Vectors.dense(1, 1.2, 1.3), 1, False),
                    (Vectors.dense(14.0, 15.0, 16.0), 0, False),
                    (Vectors.dense(1.1, 1.2, 1.3), 1, True),
                ],
                ["features", "label", "val_col"],
            )
            classifier = SparkXGBClassifier(
                num_workers=2,
                tree_method=tree_method,
                min_child_weight=0.0,
                reg_alpha=0,
                reg_lambda=0,
                validation_indicator_col="val_col",
            )
            model = classifier.fit(df_train)
            pred_result = model.transform(df_train).collect()
            for row in pred_result:
                self.assertEqual(row.prediction, row.label)

    def test_empty_partition(self):
        # raw_df.repartition(4) will result int severe data skew, actually,
        # there is no any data in reducer partition 1, reducer partition 2
        # see https://github.com/dmlc/xgboost/issues/8221
        for tree_method in [
            "hist",
            "approx",
        ]:  # pytest.mark conflict with python unittest
            raw_df = self.session.range(0, 100, 1, 50).withColumn(
                "label",
                spark_sql_func.when(spark_sql_func.rand(1) > 0.5, 1).otherwise(0),
            )
            vector_assembler = (
                VectorAssembler().setInputCols(["id"]).setOutputCol("features")
            )
            data_trans = vector_assembler.setHandleInvalid("keep").transform(raw_df)

            classifier = SparkXGBClassifier(num_workers=4, tree_method=tree_method)
            classifier.fit(data_trans)

    def test_unsupported_params(self):
        with pytest.raises(ValueError, match="evals_result"):
            SparkXGBClassifier(evals_result={})

    def test_collective_conf(self):
        classifier = SparkXGBClassifier(
            launch_tracker_on_driver=True,
            coll_cfg=Config(tracker_host_ip="192.168.1.32", tracker_port=59981),
        )
        with pytest.raises(Exception, match="Failed to bind socket"):
            classifier._get_tracker_args()

        classifier = SparkXGBClassifier(
            launch_tracker_on_driver=False,
            coll_cfg=Config(tracker_host_ip="127.0.0.1", tracker_port=58892),
        )
        with pytest.raises(
            ValueError, match="You must enable launch_tracker_on_driver"
        ):
            classifier._get_tracker_args()

        classifier = SparkXGBClassifier(
            launch_tracker_on_driver=True,
            coll_cfg=Config(tracker_host_ip="127.0.0.1", tracker_port=58893),
            num_workers=2,
        )
        launch_tracker_on_driver, rabit_envs = classifier._get_tracker_args()
        assert launch_tracker_on_driver is True
        assert rabit_envs["n_workers"] == 2
        assert rabit_envs["dmlc_tracker_uri"] == "127.0.0.1"
        assert rabit_envs["dmlc_tracker_port"] == 58893

        with tempfile.TemporaryDirectory() as tmpdir:
            path = "file:" + tmpdir
            port = get_avail_port()
            classifier = SparkXGBClassifier(
                launch_tracker_on_driver=True,
                coll_cfg=Config(tracker_host_ip="127.0.0.1", tracker_port=port),
                num_workers=1,
                n_estimators=1,
            )

            def check_conf(conf: Config) -> None:
                assert conf.tracker_host_ip == "127.0.0.1"
                assert conf.tracker_port == port

            check_conf(classifier.getOrDefault(classifier.coll_cfg))
            classifier.write().overwrite().save(path)

            loaded_classifier = SparkXGBClassifier.load(path)
            check_conf(loaded_classifier.getOrDefault(loaded_classifier.coll_cfg))

            model = classifier.fit(self.cls_df_sparse_train)
            check_conf(model.getOrDefault(model.coll_cfg))

            model.write().overwrite().save(path)
            loaded_model = SparkXGBClassifierModel.load(path)
            check_conf(loaded_model.getOrDefault(loaded_model.coll_cfg))

    def test_classifier_with_multi_cols(self):
        df = self.session.createDataFrame(
            [
                (1.0, 2.0, 0),
                (3.1, 4.2, 1),
            ],
            ["a", "b", "label"],
        )
        features = ["a", "b"]
        cls = SparkXGBClassifier(features_col=features, device="cpu", n_estimators=2)
        model = cls.fit(df)
        self.assertEqual(features, model.getOrDefault(model.features_cols))
        self.assertTrue(not model.isSet(model.featuresCol))

        # No exception
        model.transform(df).collect()


LTRData = namedtuple(
    "LTRData",
    (
        "df_train",
        "df_test",
        "df_train_1",
        "ranker_df_merged",
        "expected_evals_result_train",
        "expected_evals_result_validation",
    ),
)


@pytest.fixture
def ltr_data(spark: SparkSession) -> Generator[LTRData, None, None]:
    spark.conf.set("spark.sql.execution.arrow.maxRecordsPerBatch", "8")
    ranker_df_train = spark.createDataFrame(
        [
            (Vectors.dense(1.0, 2.0, 3.0), 0, 0),
            (Vectors.dense(4.0, 5.0, 6.0), 1, 0),
            (Vectors.dense(9.0, 4.0, 8.0), 2, 0),
            (Vectors.sparse(3, {1: 1.0, 2: 5.5}), 0, 1),
            (Vectors.sparse(3, {1: 6.0, 2: 7.5}), 1, 1),
            (Vectors.sparse(3, {1: 8.0, 2: 9.5}), 2, 1),
        ],
        ["features", "label", "qid"],
    )
    X_train = np.array(
        [
            [1.0, 2.0, 3.0],
            [4.0, 5.0, 6.0],
            [9.0, 4.0, 8.0],
            [np.nan, 1.0, 5.5],
            [np.nan, 6.0, 7.5],
            [np.nan, 8.0, 9.5],
        ]
    )
    qid_train = np.array([0, 0, 0, 1, 1, 1])
    y_train = np.array([0, 1, 2, 0, 1, 2])

    X_test = np.array(
        [
            [1.5, 2.0, 3.0],
            [4.5, 5.0, 6.0],
            [9.0, 4.5, 8.0],
            [np.nan, 1.0, 6.0],
            [np.nan, 6.0, 7.0],
            [np.nan, 8.0, 10.5],
        ]
    )
    qid_test = np.array([0, 0, 0, 1, 1, 1])
    y_test = np.array([1, 0, 2, 1, 1, 2])

    ltr = xgb.XGBRanker(tree_method="approx", objective="rank:pairwise")
    ltr.fit(X_train, y_train, qid=qid_train)
    predt = ltr.predict(X_test)

    ltr2 = xgb.XGBRanker(tree_method="approx", objective="rank:pairwise")
    ltr2.fit(
        X_train,
        y_train,
        qid=qid_train,
        eval_set=[(X_train, y_train), (X_test, y_test)],
        eval_qid=[qid_train, qid_test],
    )
    evals_result = ltr2.evals_result()
    expected_evals_result_train = evals_result["validation_0"]["ndcg@32"]
    expected_evals_result_validation = evals_result["validation_1"]["ndcg@32"]

    ranker_df_test = spark.createDataFrame(
        [
            (Vectors.dense(1.5, 2.0, 3.0), 0, float(predt[0]), 1),
            (Vectors.dense(4.5, 5.0, 6.0), 0, float(predt[1]), 0),
            (Vectors.dense(9.0, 4.5, 8.0), 0, float(predt[2]), 2),
            (Vectors.sparse(3, {1: 1.0, 2: 6.0}), 1, float(predt[3]), 1),
            (Vectors.sparse(3, {1: 6.0, 2: 7.0}), 1, float(predt[4]), 1),
            (Vectors.sparse(3, {1: 8.0, 2: 10.5}), 1, float(predt[5]), 2),
        ],
        ["features", "qid", "expected_prediction", "label"],
    )

    ranker_df_merged = (
        ranker_df_train.select(["features", "label", "qid"])
        .withColumn("isVal", spark_sql_func.lit(False))
        .union(
            ranker_df_test.select(["features", "label", "qid"]).withColumn(
                "isVal", spark_sql_func.lit(True)
            )
        )
    )

    ranker_df_train_1 = spark.createDataFrame(
        [
            (Vectors.sparse(3, {1: 1.0, 2: 5.5}), 0, 9),
            (Vectors.sparse(3, {1: 6.0, 2: 7.5}), 1, 9),
            (Vectors.sparse(3, {1: 8.0, 2: 9.5}), 2, 9),
            (Vectors.dense(1.0, 2.0, 3.0), 0, 8),
            (Vectors.dense(4.0, 5.0, 6.0), 1, 8),
            (Vectors.dense(9.0, 4.0, 8.0), 2, 8),
            (Vectors.sparse(3, {1: 1.0, 2: 5.5}), 0, 7),
            (Vectors.sparse(3, {1: 6.0, 2: 7.5}), 1, 7),
            (Vectors.sparse(3, {1: 8.0, 2: 9.5}), 2, 7),
            (Vectors.dense(1.0, 2.0, 3.0), 0, 6),
            (Vectors.dense(4.0, 5.0, 6.0), 1, 6),
            (Vectors.dense(9.0, 4.0, 8.0), 2, 6),
        ]
        * 4,
        ["features", "label", "qid"],
    )
    yield LTRData(
        ranker_df_train,
        ranker_df_test,
        ranker_df_train_1,
        ranker_df_merged,
        expected_evals_result_train,
        expected_evals_result_validation,
    )


class TestPySparkLocalLETOR:
    def test_ranker(self, ltr_data: LTRData) -> None:
        ranker = SparkXGBRanker(qid_col="qid", objective="rank:pairwise")
        assert ranker.getOrDefault(ranker.objective) == "rank:pairwise"
        model = ranker.fit(ltr_data.df_train)
        pred_result = model.transform(ltr_data.df_test).collect()
        for row in pred_result:
            assert np.isclose(row.prediction, row.expected_prediction, rtol=1e-3)

    def test_ranker_qid_sorted(self, ltr_data: LTRData) -> None:
        ranker = SparkXGBRanker(qid_col="qid", num_workers=4, objective="rank:ndcg")
        assert ranker.getOrDefault(ranker.objective) == "rank:ndcg"
        model = ranker.fit(ltr_data.df_train_1)
        model.transform(ltr_data.df_test).collect()

    def test_ranker_same_qid_in_same_partition(self, ltr_data: LTRData) -> None:
        ranker = SparkXGBRanker(qid_col="qid", num_workers=4, force_repartition=True)
        df, _ = ranker._prepare_input(ltr_data.df_train_1)

        def f(iterator: Iterable) -> List[int]:
            yield list(set(iterator))

        rows = df.select("qid").rdd.mapPartitions(f).collect()
        assert len(rows) == 4
        for row in rows:
            assert len(row) == 1
            assert row[0].qid in [6, 7, 8, 9]

    def test_ranker_xgb_summary(self, ltr_data: LTRData) -> None:
        spark_xgb_model = SparkXGBRanker(
            tree_method="approx", qid_col="qid", objective="rank:pairwise"
        ).fit(ltr_data.df_train)

        np.testing.assert_allclose(
            ltr_data.expected_evals_result_train,
            spark_xgb_model.training_summary.train_objective_history["ndcg@32"],
            atol=1e-3,
        )

        assert spark_xgb_model.training_summary.validation_objective_history == {}

    def test_ranker_xgb_summary_with_validation(self, ltr_data: LTRData) -> None:
        spark_xgb_model = SparkXGBRanker(
            tree_method="approx",
            qid_col="qid",
            objective="rank:pairwise",
            validation_indicator_col="isVal",
        ).fit(ltr_data.ranker_df_merged)

        np.testing.assert_allclose(
            ltr_data.expected_evals_result_train,
            spark_xgb_model.training_summary.train_objective_history["ndcg@32"],
            atol=1e-3,
        )

        np.testing.assert_allclose(
            ltr_data.expected_evals_result_validation,
            spark_xgb_model.training_summary.validation_objective_history["ndcg@32"],
            atol=1e-3,
        )
