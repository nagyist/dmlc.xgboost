/*
 Copyright (c) 2014-2023 by Contributors

 Licensed under the Apache License, Version 2.0 (the "License");
 you may not use this file except in compliance with the License.
 You may obtain a copy of the License at

 http://www.apache.org/licenses/LICENSE-2.0

 Unless required by applicable law or agreed to in writing, software
 distributed under the License is distributed on an "AS IS" BASIS,
 WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 See the License for the specific language governing permissions and
 limitations under the License.
 */
package ml.dmlc.xgboost4j.java;

import java.io.*;
import java.util.Arrays;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

import com.esotericsoftware.kryo.Kryo;
import com.esotericsoftware.kryo.KryoSerializable;
import com.esotericsoftware.kryo.io.Input;
import com.esotericsoftware.kryo.io.Output;
import org.apache.commons.logging.Log;
import org.apache.commons.logging.LogFactory;

/**
 * Booster for xgboost, this is a model API that support interactive build of an XGBoost Model
 */
public class Booster implements Serializable, KryoSerializable {
  public static final String DEFAULT_FORMAT = "ubj";
  private static final Log logger = LogFactory.getLog(Booster.class);
  // handle to the booster.
  private long handle = 0;
  private int version = 0;

  /**
   * This enumeration defines the type of prediction to be made and is used for inplace predictions.
   * Type of prediction, used for inplace_predict.
   */
  public enum PredictionType {
    kValue(0),
    kMargin(1);

    private Integer ptype;
    private PredictionType(final Integer ptype) {
      this.ptype = ptype;
    }
    public Integer getPType() {
      return ptype;
    }
  }

  /**
   * Create a new Booster with empty stage.
   *
   * @param params  Model parameters that are used to build the Booster
   * @param cacheMats Cached DMatrix entries that help increase the speed of Booster prediction
   *
   * @throws XGBoostError native error
   */
  Booster(Map<String, Object> params, DMatrix[] cacheMats) throws XGBoostError {
    init(cacheMats);
    setParams(params);
  }

  /**
   * Load a new Booster model from modelPath
   * @param modelPath model path
   * @return The created Booster.
   * @throws XGBoostError
   */
  static Booster loadModel(String modelPath) throws XGBoostError {
    if (modelPath == null) {
      throw new NullPointerException("modelPath : null");
    }
    Booster ret = new Booster(new HashMap<>(), new DMatrix[0]);
    XGBoostJNI.checkCall(XGBoostJNI.XGBoosterLoadModel(ret.handle, modelPath));
    return ret;
  }

  /**
   * Load a new Booster model from a byte array buffer.
   * The assumption is the array only contains one XGBoost Model.
   * This can be used to load existing booster models saved by other xgboost bindings.
   *
   * @param buffer The byte contents of the booster.
   * @return The created booster.
   * @throws XGBoostError
   */
  static Booster loadModel(byte[] buffer) throws XGBoostError {
    Booster ret = new Booster(new HashMap<>(), new DMatrix[0]);
    XGBoostJNI.checkCall(XGBoostJNI.XGBoosterLoadModelFromBuffer(ret.handle, buffer));
    return ret;
  }

  /**
   * Set parameter to the Booster.
   *
   * @param key   param name
   * @param value param value
   * @throws XGBoostError native error
   */
  public final void setParam(String key, Object value) throws XGBoostError {
    XGBoostJNI.checkCall(XGBoostJNI.XGBoosterSetParam(handle, key, value.toString()));
  }

  /**
   * Set parameters to the Booster.
   *
   * @param params parameters key-value map
   * @throws XGBoostError native error
   */
  public void setParams(Map<String, Object> params) throws XGBoostError {
    if (params != null) {
      for (Map.Entry<String, Object> entry : params.entrySet()) {
        setParam(entry.getKey(), entry.getValue().toString());
      }
    }
  }

  /**
   * Get attributes stored in the Booster as a Map.
   *
   * @return A map contain attribute pairs.
   * @throws XGBoostError native error
   */
  public final Map<String, String> getAttrs() throws XGBoostError {
    String[][] attrNames = new String[1][];
    XGBoostJNI.checkCall(XGBoostJNI.XGBoosterGetAttrNames(handle, attrNames));
    Map<String, String> attrMap = new HashMap<>();
    for (String name: attrNames[0]) {
      attrMap.put(name, this.getAttr(name));
    }
    return attrMap;
  }

  /**
   * Get attribute value from the Booster based on the key provided.
   *
   * @param key   attribute key
   * @return attribute value
   * @throws XGBoostError native error
   */
  public final String getAttr(String key) throws XGBoostError {
    String[] attrValue = new String[1];
    XGBoostJNI.checkCall(XGBoostJNI.XGBoosterGetAttr(handle, key, attrValue));
    return attrValue[0];
  }

  /**
   * Set an attribute key-value pair to the Booster.
   *
   * @param key   attribute key
   * @param value attribute value
   * @throws XGBoostError native error
   */
  public final void setAttr(String key, String value) throws XGBoostError {
    XGBoostJNI.checkCall(XGBoostJNI.XGBoosterSetAttr(handle, key, value));
  }

  /**
   * Set multiple attribute key-value pairs to the Booster.
   *
   * @param attrs attributes key-value map
   * @throws XGBoostError native error
   */
  public void setAttrs(Map<String, String> attrs) throws XGBoostError {
    if (attrs != null) {
      for (Map.Entry<String, String> entry : attrs.entrySet()) {
        setAttr(entry.getKey(), entry.getValue());
      }
    }
  }

  /**
   * Get all the feature names from the Booster.
   * @return An array of all the feature names.
   * @throws XGBoostError
   */
  public final String[] getFeatureNames() throws XGBoostError {
    int numFeature = (int) getNumFeature();
    String[] out = new String[numFeature];
    XGBoostJNI.checkCall(XGBoostJNI.XGBoosterGetStrFeatureInfo(handle, "feature_name", out));
    return out;
  }

  /**
   * Set feature names to the Booster.
   *
   * @param featureNames An array of all the feature names.
   * @throws XGBoostError
   */
  public void setFeatureNames(String[] featureNames) throws XGBoostError {
    XGBoostJNI.checkCall(XGBoostJNI.XGBoosterSetStrFeatureInfo(
        handle, "feature_name", featureNames));
  }

  /**
   * Get feature types from the Booster.
   * @return An array of all the feature types.
   * @throws XGBoostError
   */
  public final String[] getFeatureTypes() throws XGBoostError {
    int numFeature = (int) getNumFeature();
    String[] out = new String[numFeature];
    XGBoostJNI.checkCall(XGBoostJNI.XGBoosterGetStrFeatureInfo(handle, "feature_type", out));
    return out;
  }

  /**
   * Set feature types to the Booster.
   * @param featureTypes An array of all the feature types.
   * @throws XGBoostError
   */
  public void setFeatureTypes(String[] featureTypes) throws XGBoostError {
    XGBoostJNI.checkCall(XGBoostJNI.XGBoosterSetStrFeatureInfo(
        handle, "feature_type", featureTypes));
  }

  /**
   * Update the booster for one iteration.
   *
   * @param dtrain training data
   * @param iter   current iteration number
   * @throws XGBoostError native error
   */
  public void update(DMatrix dtrain, int iter) throws XGBoostError {
    XGBoostJNI.checkCall(XGBoostJNI.XGBoosterUpdateOneIter(handle, iter, dtrain.getHandle()));
  }

  @Deprecated
  public void update(DMatrix dtrain, IObjective obj) throws XGBoostError {
    float[][] predicts = this.predict(dtrain, true, 0, false, false);
    List<float[]> gradients = obj.getGradient(predicts, dtrain);
    this.boost(dtrain, gradients.get(0), gradients.get(1));
  }

  /**
   * Update with customize object functon
   *
   * @param dtrain training data
   * @param iter   The current training iteration.
   * @param obj    customized objective class
   * @throws XGBoostError native error
   */
  public void update(DMatrix dtrain, int iter, IObjective obj) throws XGBoostError {
    float[][] predicts = this.predict(dtrain, true, 0, false, false);
    List<float[]> gradients = obj.getGradient(predicts, dtrain);
    this.boost(dtrain, iter, gradients.get(0), gradients.get(1));
  }

  @Deprecated
  public void boost(DMatrix dtrain, float[] grad, float[] hess) throws XGBoostError {
    this.boost(dtrain, 0, grad, hess);
  }

  /**
   * Update with give grad and hess
   *
   * @param dtrain training data
   * @param iter   The current training iteration.
   * @param grad   first order of gradient
   * @param hess   seconde order of gradient
   * @throws XGBoostError native error
   */
  public void boost(DMatrix dtrain, int iter, float[] grad, float[] hess) throws XGBoostError {
    if (grad.length != hess.length) {
      throw new AssertionError(String.format("grad/hess length mismatch %s / %s", grad.length,
              hess.length));
    }
    XGBoostJNI.checkCall(XGBoostJNI.XGBoosterTrainOneIter(handle,
                                                          dtrain.getHandle(), iter, grad, hess));
  }

  /**
   * Evaluate the Booster model with given dmatrixs.
   *
   * @param evalMatrixs dmatrixs for evaluation
   * @param evalNames   name for eval dmatrixs, used for check results
   * @param iter        current eval iteration
   * @return eval Information containing the evaluation results
   * @throws XGBoostError native error
   */
  public String evalSet(DMatrix[] evalMatrixs, String[] evalNames, int iter) throws XGBoostError {
    long[] handles = dmatrixsToHandles(evalMatrixs);
    String[] evalInfo = new String[1];
    XGBoostJNI.checkCall(XGBoostJNI.XGBoosterEvalOneIter(handle, iter, handles, evalNames,
            evalInfo));
    return evalInfo[0];
  }

  /**
   * Evaluate the Booster model with given dmatrixs.
   *
   * @param evalMatrixs dmatrixs for evaluation
   * @param evalNames   name for eval dmatrixs, used for check results
   * @param iter        current eval iteration
   * @param metricsOut  output array containing the evaluation metrics for each evalMatrix
   * @return eval Information containing the evaluation results
   * @throws XGBoostError native error
   */
  public String evalSet(DMatrix[] evalMatrixs, String[] evalNames, int iter, float[] metricsOut)
          throws XGBoostError {
    String stringFormat = evalSet(evalMatrixs, evalNames, iter);
    String[] metricPairs = stringFormat.split("\t");
    for (int i = 1; i < metricPairs.length; i++) {
      String value = metricPairs[i].split(":")[1];
      if (value.equalsIgnoreCase("nan")) {
        metricsOut[i - 1] = Float.NaN;
      } else if (value.equalsIgnoreCase("-nan")) {
        metricsOut[i - 1] = -Float.NaN;
      } else {
        metricsOut[i - 1] = Float.valueOf(value);
      }
    }
    return stringFormat;
  }

  /**
   * Evaluate the Booster model given customized Evaluation class
   *
   * @param evalMatrixs evaluation matrix
   * @param evalNames   evaluation names
   * @param eval        custom evaluator
   * @return eval Information containing the evaluation results
   * @throws XGBoostError native error
   */
  public String evalSet(DMatrix[] evalMatrixs, String[] evalNames, IEvaluation eval)
          throws XGBoostError {
    // Hopefully, a tiny redundant allocation wouldn't hurt.
    return evalSet(evalMatrixs, evalNames, eval, new float[evalNames.length]);
  }

  public String evalSet(DMatrix[] evalMatrixs, String[] evalNames, IEvaluation eval,
                        float[] metricsOut) throws XGBoostError {
    String evalInfo = "";
    for (int i = 0; i < evalNames.length; i++) {
      String evalName = evalNames[i];
      DMatrix evalMat = evalMatrixs[i];
      float evalResult = eval.eval(predict(evalMat), evalMat);
      String evalMetric = eval.getMetric();
      evalInfo += String.format("\t%s-%s:%f", evalName, evalMetric, evalResult);
      metricsOut[i] = evalResult;
    }
    return evalInfo;
  }

  /**
   * An advanced prediction function with all the options.
   *
   * @param data         the test data for which prodictions are to be made
   * @param outputMargin output margin
   * @param treeLimit    limit number of trees, 0 means all trees.
   * @param predLeaf     prediction minimum to keep leafs
   * @param predContribs prediction feature contributions
   * @return predict two dimensional array of results, where each row corresponds to a prediction.
   */
  private synchronized float[][] predict(DMatrix data,
                                         boolean outputMargin,
                                         int treeLimit,
                                         boolean predLeaf,
                                         boolean predContribs) throws XGBoostError {
    int optionMask = 0;
    if (outputMargin) {
      optionMask = 1;
    }
    if (predLeaf) {
      optionMask = 2;
    }
    if (predContribs) {
      optionMask = 4;
    }
    float[][] rawPredicts = new float[1][];
    XGBoostJNI.checkCall(XGBoostJNI.XGBoosterPredict(handle, data.getHandle(), optionMask,
            treeLimit, rawPredicts));
    int row = (int) data.rowNum();
    int col = rawPredicts[0].length / row;
    float[][] predicts = new float[row][col];
    int r, c;
    for (int i = 0; i < rawPredicts[0].length; i++) {
      r = i / col;
      c = i % col;
      predicts[r][c] = rawPredicts[0][i];
    }
    return predicts;
  }

  /**
   * Perform thread-safe prediction.
   *
   * @param data      Flattened input matrix of features for prediction
   * @param nrow      The number of preditions to make (count of input matrix rows)
   * @param ncol      The number of features in the model (count of input matrix columns)
   * @param missing   Value indicating missing element in the <code>data</code> input matrix
   *
   * @return predict  Result matrix
   */
  public float[][] inplace_predict(float[] data,
                                   int nrow,
                                   int ncol,
                                   float missing) throws XGBoostError {
    int[] iteration_range = new int[2];
    iteration_range[0] = 0;
    iteration_range[1] = 0;
    return this.inplace_predict(data, nrow, ncol,
        missing, iteration_range, PredictionType.kValue, null);
  }

  /**
   * Perform thread-safe prediction.
   *
   * @param data      Flattened input matrix of features for prediction
   * @param nrow      The number of preditions to make (count of input matrix rows)
   * @param ncol      The number of features in the model (count of input matrix columns)
   * @param missing   Value indicating missing element in the <code>data</code> input matrix
   * @param iteration_range Specifies which layer of trees are used in prediction.  For
   *                        example, if a random forest is trained with 100 rounds.
   *                        Specifying `iteration_range=[10, 20)`, then only the forests
   *                        built during [10, 20) (half open set) rounds are used in this
   *                        prediction.
   *
   * @return predict  Result matrix
   */
  public float[][] inplace_predict(float[] data,
                                   int nrow,
                                   int ncol,
                                   float missing, int[] iteration_range) throws XGBoostError {
    return this.inplace_predict(data, nrow, ncol,
        missing, iteration_range, PredictionType.kValue, null);
  }


  /**
   * Perform thread-safe prediction.
   *
   * @param data            Flattened input matrix of features for prediction
   * @param nrow            The number of preditions to make (count of input matrix rows)
   * @param ncol            The number of features in the model (count of input matrix columns)
   * @param missing         Value indicating missing element in the <code>data</code> input matrix
   * @param iteration_range Specifies which layer of trees are used in prediction.  For
   *                        example, if a random forest is trained with 100 rounds.
   *                        Specifying `iteration_range=[10, 20)`, then only the forests
   *                        built during [10, 20) (half open set) rounds are used in this
   *                        prediction.
   * @param predict_type    What kind of prediction to run.
   * @return predict       Result matrix
   */
  public float[][] inplace_predict(float[] data,
                                   int nrow,
                                   int ncol,
                                   float missing,
                                   int[] iteration_range,
                                   PredictionType predict_type,
                                   float[] base_margin) throws XGBoostError {
    if (iteration_range.length != 2) {
      throw new XGBoostError(new String("Iteration range is expected to be [begin, end)."));
    }
    int ptype = predict_type.getPType();

    int begin = iteration_range[0];
    int end = iteration_range[1];

    float[][] rawPredicts = new float[1][];
    XGBoostJNI.checkCall(XGBoostJNI.XGBoosterPredictFromDense(handle, data, nrow, ncol,
        missing,
        begin, end, ptype, base_margin, rawPredicts));

    int col = rawPredicts[0].length / nrow;
    float[][] predicts = new float[nrow][col];
    int r, c;
    for (int i = 0; i < rawPredicts[0].length; i++) {
      r = i / col;
      c = i % col;
      predicts[r][c] = rawPredicts[0][i];
    }
    return predicts;
  }

  /**
   * Predict leaf indices given the data
   *
   * @param data The input data.
   * @param treeLimit Number of trees to include, 0 means all trees.
   * @return The leaf indices of the instance.
   * @throws XGBoostError
   */
  public float[][] predictLeaf(DMatrix data, int treeLimit) throws XGBoostError {
    return this.predict(data, false, treeLimit, true, false);
  }

  /**
   * Output feature contributions toward predictions of given data
   *
   * @param data The input data.
   * @param treeLimit Number of trees to include, 0 means all trees.
   * @return The feature contributions and bias.
   * @throws XGBoostError
   */
  public float[][] predictContrib(DMatrix data, int treeLimit) throws XGBoostError {
    return this.predict(data, false, treeLimit, true, true);
  }

  /**
   * Make a prediction with test data in a DMatrix format.
   *
   * @param data dmatrix storing the test input on which predictions are to be made
   * @return predict The results of the prediction, where each row corresponds to a prediction.
   * @throws XGBoostError native error
   */
  public float[][] predict(DMatrix data) throws XGBoostError {
    return this.predict(data, false, 0, false, false);
  }

  /**
   * Make a prediction with test data in a DMatrix format and output margin.
   *
   * @param data  dmatrix storing the test input on which predictions are to be made
   * @param outputMargin output margin
   * @return predict The results of the prediction, where each row corresponds to a prediction.
   */
  public float[][] predict(DMatrix data, boolean outputMargin) throws XGBoostError {
    return this.predict(data, outputMargin, 0, false, false);
  }

  /**
   * Advanced predict function with all the options.
   *
   * @param data         matrix storing the test input on which predictions are to be made
   * @param outputMargin output margin
   * @param treeLimit    limit number of trees, 0 means all trees.
   * @return predict The results of the prediction, where each row corresponds to a prediction.
   */
  public float[][] predict(DMatrix data, boolean outputMargin, int treeLimit) throws XGBoostError {
    return this.predict(data, outputMargin, treeLimit, false, false);
  }

  /**
   * Save model to modelPath
   *
   * @param modelPath model path
   */
  public void saveModel(String modelPath) throws XGBoostError{
    XGBoostJNI.checkCall(XGBoostJNI.XGBoosterSaveModel(handle, modelPath));
  }

  /**
   * Save the model to file opened as output stream.
   * The model format is compatible with other xgboost bindings.
   * The output stream can only save one xgboost model.
   * This function will close the OutputStream after the save.
   *
   * @param out The output stream
   */
  public void saveModel(OutputStream out) throws XGBoostError, IOException {
    saveModel(out, DEFAULT_FORMAT);
  }

  /**
   * Save the model to file opened as output stream.
   * The model format is compatible with other xgboost bindings.
   * The output stream can only save one xgboost model.
   * This function will close the OutputStream after the save.
   *
   * @param out The output stream
   * @param format The model format (ubj, json, deprecated)
   * @throws XGBoostError
   * @throws IOException
   */
  public void saveModel(OutputStream out, String format) throws XGBoostError, IOException {
    out.write(this.toByteArray(format));
    out.close();
  }

  /**
   * Get the dump of the model as a string array
   *
   * @param featureMap A string containing the path to a feature map.
   * @param withStats Controls whether the split statistics are output.
   * @return The dumped model information
   * @throws XGBoostError native error
   */
  public String[] getModelDump(String featureMap, boolean withStats) throws XGBoostError {
    return getModelDump(featureMap, withStats, "text");
  }

  /**
   * Get the dump of the model as a string array with specified feature map, stats,
   * and the specified format.
   *
   * @param featureMap A string containing the path to a feature map.
   * @param withStats Controls whether the split statistics are output.
   * @param format The format in which the model is dumped (text, json, ubj).
   * @return The dumped model information
   * @throws XGBoostError
   */
  public String[] getModelDump(String featureMap, boolean withStats, String format)
         throws XGBoostError {
    int statsFlag = 0;
    if (featureMap == null) {
      featureMap = "";
    }
    if (withStats) {
      statsFlag = 1;
    }
    if (format == null) {
      format = "text";
    }
    String[][] modelInfos = new String[1][];
    XGBoostJNI.checkCall(
            XGBoostJNI.XGBoosterDumpModelEx(handle, featureMap, statsFlag, format, modelInfos));
    return modelInfos[0];
  }

  /**
   * Get the dump of the model as a string array with specified feature names.
   *
   * @param featureNames Names of the features.
   * @return dumped model information
   * @throws XGBoostError
   */
  public String[] getModelDump(String[] featureNames, boolean withStats) throws XGBoostError {
    return getModelDump(featureNames, withStats, "text");
  }

  /**
   * Get the dump of the model as a string array with specified feature map, stats,
   * and the specified format.
   *
   * @param featureNames An array of strings containing the feature names.
   * @param withStats Controls whether the split statistics are output.
   * @param format The format in which the model is dumped (text, json, ubj).
   * @return The dumped model information
   * @throws XGBoostError
   */
  public String[] getModelDump(String[] featureNames, boolean withStats, String format)
      throws XGBoostError {
    int statsFlag = 0;
    if (withStats) {
      statsFlag = 1;
    }
    if (format == null) {
      format = "text";
    }
    String[][] modelInfos = new String[1][];
    XGBoostJNI.checkCall(XGBoostJNI.XGBoosterDumpModelExWithFeatures(
        handle, featureNames, statsFlag, format, modelInfos));
    return modelInfos[0];
  }

  /**
   * Supported feature importance types
   *
   * WEIGHT = Number of nodes that a feature was used to determine a split
   * GAIN = Average information gain per split for a feature
   * COVER = Average cover per split for a feature
   * TOTAL_GAIN = Total information gain over all splits of a feature
   * TOTAL_COVER = Total cover over all splits of a feature
   */
  public static class FeatureImportanceType {
    public static final String WEIGHT = "weight";
    public static final String GAIN = "gain";
    public static final String COVER = "cover";
    public static final String TOTAL_GAIN = "total_gain";
    public static final String TOTAL_COVER = "total_cover";
    public static final Set<String> ACCEPTED_TYPES = new HashSet<>(
            Arrays.asList(WEIGHT, GAIN, COVER, TOTAL_GAIN, TOTAL_COVER));
  }

  /**
   * Get importance of each feature with specified feature names.
   *
   * @return featureScoreMap  key: feature name, value: feature importance score, can be nill.
   * @throws XGBoostError native error
   */
  public Map<String, Integer> getFeatureScore(String[] featureNames) throws XGBoostError {
    String[] modelInfos = getModelDump(featureNames, false);
    return getFeatureWeightsFromModel(modelInfos);
  }

  /**
   * Get importance of each feature
   *
   * @return featureScoreMap  key: feature index, value: feature importance score, can be nill
   * @throws XGBoostError native error
   */
  public Map<String, Integer> getFeatureScore(String featureMap) throws XGBoostError {
    String[] modelInfos = getModelDump(featureMap, false);
    return getFeatureWeightsFromModel(modelInfos);
  }

  /**
   * Get the importance of each feature based purely on weights (number of splits)
   *
   * @return featureScoreMap key: feature index,
   * value: feature importance score based on weight
   * @throws XGBoostError native error
   */
  private Map<String, Integer> getFeatureWeightsFromModel(String[] modelInfos) throws XGBoostError {
    Map<String, Integer> featureScore = new HashMap<>();
    for (String tree : modelInfos) {
      for (String node : tree.split("\n")) {
        String[] array = node.split("\\[");
        if (array.length == 1) {
          continue;
        }
        String fid = array[1].split("\\]")[0];
        fid = fid.split("<")[0];
        if (featureScore.containsKey(fid)) {
          featureScore.put(fid, 1 + featureScore.get(fid));
        } else {
          featureScore.put(fid, 1);
        }
      }
    }
    return featureScore;
  }

  /**
   * Get the feature importances for gain or cover (average or total)
   *
   * @return featureImportanceMap key: feature index,
   * values: feature importance score based on gain or cover
   * @throws XGBoostError native error
   */
  public Map<String, Double> getScore(
          String[] featureNames, String importanceType) throws XGBoostError {
    String[] modelInfos = getModelDump(featureNames, true);
    return getFeatureImportanceFromModel(modelInfos, importanceType);
  }

  /**
   * Get the feature importances for gain or cover (average or total), with feature names
   *
   * @return featureImportanceMap key: feature name,
   * values: feature importance score based on gain or cover
   * @throws XGBoostError native error
   */
  public Map<String, Double> getScore(
          String featureMap, String importanceType) throws XGBoostError {
    String[] modelInfos = getModelDump(featureMap, true);
    return getFeatureImportanceFromModel(modelInfos, importanceType);
  }

  /**
   * Get the importance of each feature based on information gain or cover
   *
   * @return featureImportanceMap key: feature index, value: feature importance score
   * based on information gain or cover
   * @throws XGBoostError native error
   */
  private Map<String, Double> getFeatureImportanceFromModel(
          String[] modelInfos, String importanceType) throws XGBoostError {
    if (!FeatureImportanceType.ACCEPTED_TYPES.contains(importanceType)) {
      throw new AssertionError(String.format("Importance type %s is not supported",
              importanceType));
    }
    Map<String, Double> importanceMap = new HashMap<>();
    Map<String, Double> weightMap = new HashMap<>();
    if (importanceType.equals(FeatureImportanceType.WEIGHT)) {
      Map<String, Integer> importanceWeights = getFeatureWeightsFromModel(modelInfos);
      for (String feature: importanceWeights.keySet()) {
        importanceMap.put(feature, new Double(importanceWeights.get(feature)));
      }
      return importanceMap;
    }
    /* Each split in the tree has this text form:
    "0:[f28<-9.53674316e-07] yes=1,no=2,missing=1,gain=4000.53101,cover=1628.25"
    So the line has to be split according to whether cover or gain is desired */
    String splitter = "gain=";
    if (importanceType.equals(FeatureImportanceType.COVER)
        || importanceType.equals(FeatureImportanceType.TOTAL_COVER)) {
      splitter = "cover=";
    }
    for (String tree: modelInfos) {
      for (String node: tree.split("\n")) {
        String[] array = node.split("\\[");
        if (array.length == 1) {
          continue;
        }
        String[] fidWithImportance = array[1].split("\\]");
        // Extract gain or cover from string after closing bracket
        Double importance = Double.parseDouble(
            fidWithImportance[1].split(splitter)[1].split(",")[0]
        );
        String fid = fidWithImportance[0].split("<")[0];
        if (importanceMap.containsKey(fid)) {
          importanceMap.put(fid, importance + importanceMap.get(fid));
          weightMap.put(fid, 1d + weightMap.get(fid));
        } else {
          importanceMap.put(fid, importance);
          weightMap.put(fid, 1d);
        }
      }
    }
    /* By default we calculate total gain and total cover.
    Divide by the number of nodes per feature to get gain / cover */
    if (importanceType.equals(FeatureImportanceType.COVER)
        || importanceType.equals(FeatureImportanceType.GAIN)) {
      for (String fid: importanceMap.keySet()) {
        importanceMap.put(fid, importanceMap.get(fid)/weightMap.get(fid));
      }
    }
    return importanceMap;
  }

  /**
   * Save model into raw byte array in the UBJSON ("ubj") format.
   *
   * @return the saved byte array
   * @throws XGBoostError native error
   */
  public byte[] toByteArray() throws XGBoostError {
    return this.toByteArray(DEFAULT_FORMAT);
  }

  /**
   * Save model into raw byte array.
   *
   * @param format The output format.  Available options are "json", "ubj" and "deprecated".
   *
   * @return the saved byte array
   * @throws XGBoostError native error
   */
  public byte[] toByteArray(String format) throws XGBoostError {
    byte[][] bytes = new byte[1][];
    XGBoostJNI.checkCall(XGBoostJNI.XGBoosterSaveModelToBuffer(this.handle, format, bytes));
    return bytes[0];
  }

  /**
   * Get number of model features.
   * @return the number of features.
   * @throws XGBoostError
   */
  public long getNumFeature() throws XGBoostError {
    long[] numFeature = new long[1];
    XGBoostJNI.checkCall(XGBoostJNI.XGBoosterGetNumFeature(this.handle, numFeature));
    return numFeature[0];
  }
  public int getNumBoostedRound() throws XGBoostError {
    int[] numRound = new int[1];
    XGBoostJNI.checkCall(XGBoostJNI.XGBoosterGetNumBoostedRound(this.handle, numRound));
    return numRound[0];
  }

  /**
   * Internal initialization function.
   * @param cacheMats The cached DMatrix.
   * @throws XGBoostError
   */
  private void init(DMatrix[] cacheMats) throws XGBoostError {
    long[] handles = null;
    if (cacheMats != null) {
      handles = dmatrixsToHandles(cacheMats);
    }
    long[] out = new long[1];
    XGBoostJNI.checkCall(XGBoostJNI.XGBoosterCreate(handles, out));

    handle = out[0];
  }

  /**
   * transfer DMatrix array to handle array (used for native functions)
   *
   * @param dmatrixs
   * @return handle array for input dmatrixs
   */
  private static long[] dmatrixsToHandles(DMatrix[] dmatrixs) {
    long[] handles = new long[dmatrixs.length];
    for (int i = 0; i < dmatrixs.length; i++) {
      handles[i] = dmatrixs[i].getHandle();
    }
    return handles;
  }

  // making Booster serializable
  private void writeObject(java.io.ObjectOutputStream out) throws IOException {
    try {
      out.writeInt(version);
      out.writeObject(this.toByteArray("ubj"));
    } catch (XGBoostError ex) {
      ex.printStackTrace();
      logger.error(ex.getMessage());
    }
  }

  private void readObject(java.io.ObjectInputStream in)
          throws IOException, ClassNotFoundException {
    try {
      this.init(null);
      this.version = in.readInt();
      byte[] bytes = (byte[])in.readObject();
      XGBoostJNI.checkCall(XGBoostJNI.XGBoosterLoadModelFromBuffer(this.handle, bytes));
    } catch (XGBoostError ex) {
      ex.printStackTrace();
      logger.error(ex.getMessage());
    }
  }

  @Override
  protected void finalize() throws Throwable {
    super.finalize();
    dispose();
  }

  public synchronized void dispose() {
    if (handle != 0L) {
      XGBoostJNI.XGBoosterFree(handle);
      handle = 0;
    }
  }

  @Override
  public void write(Kryo kryo, Output output) {
    try {
      byte[] serObj = this.toByteArray("ubj");
      int serObjSize = serObj.length;
      output.writeInt(serObjSize);
      output.writeInt(version);
      output.write(serObj);
    } catch (XGBoostError ex) {
      logger.error(ex.getMessage(), ex);
    }
  }

  @Override
  public void read(Kryo kryo, Input input) {
    try {
      this.init(null);
      int serObjSize = input.readInt();
      this.version = input.readInt();
      byte[] bytes = new byte[serObjSize];
      input.readBytes(bytes);
      XGBoostJNI.checkCall(XGBoostJNI.XGBoosterLoadModelFromBuffer(this.handle, bytes));
    } catch (XGBoostError ex) {
      logger.error(ex.getMessage(), ex);
    }
  }
}
