"""
Ensemble Integration

@author: Jamie Bennett, Yan Chak (Richard) Li
"""

import pandas as pd
import numpy as np
import random
import pickle
import os
from sklearn.utils._testing import ignore_warnings
from sklearn.exceptions import ConvergenceWarning
from sklearn.metrics import precision_recall_curve
from sklearn.model_selection import StratifiedKFold
from joblib import Parallel, delayed
from imblearn.under_sampling import RandomUnderSampler
from sklearn.calibration import CalibratedClassifierCV
import warnings

warnings.filterwarnings("ignore", category=DeprecationWarning)


def fmax_score(y_true, y_pred, beta=1, display=False):
    # beta = 0 for precision, beta -> infinity for recall, beta=1 for harmonic mean
    np.seterr(divide='ignore', invalid='ignore')
    precision, recall, threshold = precision_recall_curve(y_true, y_pred)
    fmeasure = (1 + beta ** 2) * (precision * recall) / ((beta ** 2 * precision) + recall)
    argmax = np.nanargmax(fmeasure)

    f1score = fmeasure[argmax]
    pscore = precision[argmax]
    rscore = recall[argmax]

    if display:
        print("f1 score: ", f1score,
              "\nprecision score:", pscore,
              "\nrecall score:", rscore)
    return f1score, pscore, rscore


def read_arff_to_pandas_df(arff_path):
    df = pd.read_csv(arff_path, comment='@', header=None)
    columns = []
    file1 = open(arff_path, 'r')
    lines = file1.readlines()

    # Strips the newline character
    for line_idx, line in enumerate(lines):
        # if line_idx > num_col
        if '@attribute' in line.lower():
            columns.append(line.strip().split(' ')[1])

    df.columns = columns
    return df


def set_seed(random_state=1):
    random.seed(random_state)


def random_integers(n_integers=1):
    return random.sample(range(0, 10000), n_integers)


def undersample(X, y, random_state):
    RUS = RandomUnderSampler(random_state=random_state)
    X_resampled, y_resampled = RUS.fit_resample(X=X, y=y)
    return X_resampled, y_resampled


def retrieve_X_y(data):
    X = data.drop(columns=["labels"], level=0)
    y = np.ravel(data["labels"])
    return X, y


def update_keys(dictionary, string):
    return {f'{k}_' + string: v for k, v in dictionary.items()}


def append_modality(current_data, modality_data):
    combined_dataframe = []
    for fold, dataframe in enumerate(current_data):
        if (dataframe.iloc[:, -1].to_numpy() != modality_data[fold].iloc[:, -1].to_numpy()).all():
            print("Error: labels do not match across modalities")
            break
        combined_dataframe.append(pd.concat((dataframe.iloc[:, :-1],
                                             modality_data[fold]), axis=1))
    return combined_dataframe


class EnsembleIntegration:
    """
    Algorithms to test a variety of ensemble methods.

    Parameters
    ----------
    base_predictors : dictionary
        Base predictors.
    k_inner : int, optional
        Number of inner folds. Default is 5.
    random_state : int, optional
        Random state for cross-validation. The default is 42.

    Returns
    -------
    predictions_df : Pandas dataframe of shape (n_samples, n_base_predictors)
        Matrix of data intended for training of a meta-algorithm.
    """

    def __init__(self,
                 base_predictors=None,
                 meta_models=None,
                 k_outer=None,
                 k_inner=None,
                 n_bags=None,
                 bagging_strategy="mean",
                 n_jobs=-1,
                 random_state=None,
                 name="project"):

        set_seed(random_state)

        self.base_predictors = base_predictors
        self.meta_models = meta_models
        self.k_outer = k_outer
        self.k_inner = k_inner
        self.n_bags = n_bags
        self.bagging_strategy = bagging_strategy
        self.n_jobs = n_jobs
        self.random_state = random_state
        self.name = name

        self.trained_meta_models = {}
        self.trained_base_predictors = {}

        if k_outer is not None:
            self.cv_outer = StratifiedKFold(n_splits=self.k_outer, shuffle=True,
                                            random_state=random_integers(n_integers=1)[0])
        if k_inner is not None:
            self.cv_inner = StratifiedKFold(n_splits=self.k_inner, shuffle=True,
                                            random_state=random_integers(n_integers=1)[0])
        if n_bags is not None:
            self.random_numbers_for_bags = random_integers(n_integers=n_bags)

        self.meta_training_data = None
        self.meta_test_data = None

    @ignore_warnings(category=ConvergenceWarning)
    def train_meta(self, meta_models=None):

        if meta_models is not None:
            self.meta_models = meta_models

        print("\nTraining meta models \n")

        fmax_scores = []

        for fold_id in range(self.k_outer):
            for model_name, model in self.meta_models.items():

                print("\nTraining {model_name:} on outer fold {fold_id:}... \n".format(model_name=model_name,
                                                                                       fold_id=fold_id))

                X_train, y_train = retrieve_X_y(data=self.meta_training_data[fold_id])
                X_test, y_test = retrieve_X_y(data=self.meta_test_data[fold_id])

                if self.bagging_strategy == "mean":
                    X_train = X_train.groupby(level=0, axis=1).mean()
                    X_test = X_test.groupby(level=0, axis=1).mean()
                model.fit(X_train, y_train)
                y_pred = model.predict(X_test)
                fmax_scores.append(fmax_score(y_test, y_pred, display=True))

        return fmax_scores

    @ignore_warnings(category=ConvergenceWarning)
    def train_base(self, X, y, base_predictors=None, modality=None):

        if base_predictors is not None:
            self.base_predictors = base_predictors  # update base predictors

        if modality is not None:
            print(f"\n Working on {modality} data... \n")

            self.base_predictors = update_keys(dictionary=self.base_predictors,
                                               string=modality)  # include modality in model name

        if (self.meta_training_data or self.meta_test_data) is None:
            self.meta_training_data = self.train_base_inner(X, y)
            self.meta_test_data = self.train_base_outer(X, y)

        else:
            self.meta_training_data = append_modality(self.meta_training_data, self.train_base_inner(X, y))
            self.meta_test_data = append_modality(self.meta_test_data, self.train_base_outer(X, y))

        return self

    def train_base_inner(self, X, y):
        """
        Perform a round of (inner) k-fold cross validation on each outer
        training set for generation of training data for the meta-algorithm

        Parameters
        ----------
        X : array of shape (n_samples, n_features)
            Dataset.
        y : array of shape (n_samples,)
            Labels.

        Returns
        -------
        meta_training_data : List of length k_outer containing Pandas dataframes
        of shape (n_outer_training_samples, n_base_predictors * n_bags)
        """

        print("\n Training base predictors on inner training sets... \n")

        # dictionaries for meta train/test data for each outer fold
        meta_training_data = []
        # define joblib Parallel function
        parallel = Parallel(n_jobs=self.n_jobs, verbose=10)
        for outer_fold_id, (train_index_outer, test_index_outer) in enumerate(self.cv_outer.split(X, y)):
            print("\n Generating meta-training data for outer fold {outer_fold_id:}... \n".format(
                outer_fold_id=outer_fold_id))

            X_train_inner = X[train_index_outer]
            y_train_inner = y[train_index_outer]

            # spawn n_jobs jobs for each bag, inner_fold and model
            output = parallel(delayed(self.train_base_fold)(X=X_train_inner,
                                                            y=y_train_inner,
                                                            model_params=model_params,
                                                            fold_params=inner_fold_params,
                                                            bag_state=bag_state)
                              for model_params in self.base_predictors.items()
                              for inner_fold_params in enumerate(self.cv_inner.split(X_train_inner, y_train_inner)) \
                              for bag_state in enumerate(self.random_numbers_for_bags))
            combined_predictions = self.combine_data_inner(output)
            meta_training_data.append(combined_predictions)
        return meta_training_data

    def train_base_outer(self, X, y):
        """
        Train each base predictor on each outer training set

        Parameters
        ----------
        X : array of shape (n_samples, n_features)
            Dataset.
        y : array of shape (n_samples,)
            Labels.

        Returns
        -------

        meta_test_data : List of length k_outer containing Pandas dataframes
        of shape (n_outer_test_samples, n_base_predictors * n_bags)
        """

        # define joblib Parallel function
        parallel = Parallel(n_jobs=self.n_jobs, verbose=10)

        print("\n Training base predictors on outer training sets... \n")

        # spawn job for each bag, inner_fold and model
        output = parallel(delayed(self.train_base_fold)(X=X,
                                                        y=y,
                                                        model_params=model_params,
                                                        fold_params=outer_fold_params,
                                                        bag_state=bag_state)
                          for model_params in self.base_predictors.items()
                          for outer_fold_params in enumerate(self.cv_outer.split(X, y))
                          for bag_state in enumerate(self.random_numbers_for_bags))
        meta_test_data = self.combine_data_outer(output)
        return meta_test_data

    @ignore_warnings(category=ConvergenceWarning)
    def train_base_fold(self, X, y, model_params, fold_params, bag_state, outer=False):
        model_name, model = model_params
        fold_id, (train_index, test_index) = fold_params
        bag_id, bag_random_state = bag_state
        # use cross validation to calculate probability outputs
        if not hasattr(model, "predict_proba"):
            model = CalibratedClassifierCV(model)
        X_train, X_test = X[train_index], X[test_index]
        y_train, y_test = y[train_index], y[test_index]
        X_bag, y_bag = undersample(X_train, y_train, random_state=bag_random_state)
        model.fit(X_bag, y_bag)
        y_pred = model.predict_proba(X_test)[:, 1]
        f_score, _, _ = fmax_score(y_test, y_pred)
        results_dict = {"model_name": model_name, "bag_id": bag_id, "fold_id": fold_id, "fmax_score": f_score,
                        "model": model, "y_pred": y_pred, "labels": y_test}
        return results_dict

    def combine_data_inner(self, list_of_dicts):
        # dictionary to store predictions
        combined_predictions = {}
        # combine fold predictions for each model
        for model_name in self.base_predictors.keys():
            for bag_id in range(self.n_bags):
                model_predictions = np.concatenate(
                    list(d["y_pred"] for d in list_of_dicts if d["model_name"] == model_name and d["bag_id"] == bag_id))
                combined_predictions[model_name, bag_id] = model_predictions
        labels = np.concatenate(list(d["labels"] for d in list_of_dicts if
                                     d["model_name"] == list(self.base_predictors.keys())[0] and d["bag_id"] == 0))
        combined_predictions["labels", None] = labels
        combined_predictions = pd.DataFrame(combined_predictions)
        return combined_predictions

    def combine_data_outer(self, list_of_dicts):
        combined_predictions = []
        for fold_id in range(self.k_outer):
            predictions = {}
            for model_name in self.base_predictors.keys():
                for bag_id in range(self.n_bags):
                    model_predictions = list([d["y_pred"], d["model"]] for d in list_of_dicts if
                                             d["fold_id"] == fold_id and d["model_name"] == model_name and d[
                                                 "bag_id"] == bag_id)
                    predictions[model_name, bag_id] = model_predictions[0][0]
                    self.trained_base_predictors[(model_name, bag_id)] = model_predictions[0][1]
            labels = [d["labels"] for d in list_of_dicts if
                      d["fold_id"] == fold_id and d["model_name"] == list(self.base_predictors.keys())[0] and d[
                          "bag_id"] == 0]
            predictions["labels", None] = labels[0]
            combined_predictions.append(pd.DataFrame(predictions))
        return combined_predictions

    def save(self):
        with open(f"EI.{self.name}", 'wb') as f:
            pickle.dump(self, f)
        print(f"\nProject saved to EI.{self.name}\n")

    @classmethod
    def load(cls, filename):
        with open(filename, 'rb') as f:
            return pickle.load(f)
