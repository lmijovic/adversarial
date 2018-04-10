#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Script for performing comparison studies."""

# Basic import(s)
import re
import gzip

# Get ROOT to stop hogging the command-line options
import ROOT
ROOT.PyConfig.IgnoreCommandLineOptions = True

# Scientific import(s)
import numpy as np
import pandas as pd
import pickle
import root_numpy
from array import array
from scipy.stats import entropy
from sklearn.metrics import roc_curve, roc_auc_score

# Project import(s)
from adversarial.utils import initialise_backend, wpercentile, latex, parse_args, initialise, load_data, mkdir
from adversarial.profile import profile, Profile
from adversarial.constants import *
from .studies.common import *
import studies

# Custom import(s)
import rootplotting as rp


# Main function definition
@profile
def main (args):

    # Initialising
    # --------------------------------------------------------------------------
    args, cfg = initialise(args)


    # Argument-dependent setup
    # --------------------------------------------------------------------------
    # Initialise Keras backend
    initialise_backend(args)

    # Keras import(s)
    import keras.backend as K
    from keras.models import load_model

    # Project import(s)
    from adversarial.models import classifier_model, adversary_model, combined_model, decorrelation_model


    # Loading data
    # --------------------------------------------------------------------------
    data, features, _ = load_data(args.input + 'data.h5')
    data = data[data['train'] == 0]
    #data = data.sample(frac=0.01)  # @TEMP!


    # Common definitions
    # --------------------------------------------------------------------------
    eps = np.finfo(float).eps
    msk_mass = (data['m'] > 60.) & (data['m'] < 100.)  # W mass window
    msk_sig  = data['signal'] == 1
    kNN_eff = 10
    D2_kNN_var = 'D2-kNN({:d}%)'.format(kNN_eff)

    # -- Adversarial neural network (ANN) scan
    lambda_reg  = 20.
    #lambda_regs = sorted([0.1, 1, 10, 100])
    lambda_regs = sorted([0.02, 0.2, 2., 20., 200.])
    ann_vars    = list()
    lambda_strs = list()
    for lambda_reg_ in lambda_regs:
        digits = int(np.ceil(max(-np.log10(lambda_reg_), 0)))
        lambda_str = '{l:.{d:d}f}'.format(d=digits,l=lambda_reg_).replace('.', 'p')
        lambda_strs.append(lambda_str)

        ann_var_ = "ANN(#lambda={:s})".format(lambda_str.replace('p', '.'))
        ann_vars.append(ann_var_)
        pass

    ann_var = ann_vars[lambda_regs.index(lambda_reg)]

    # -- uBoost scan
    uboost_eff = 92
    uboost_ur  = 0.1
    uboost_urs = sorted([0., 0.01, 0.1, 0.3])
    #uboost_urs = sorted([0., 0.1])
    uboost_var  =  'uBoost(#alpha={:.2f})'.format(uboost_ur)
    uboost_vars = ['uBoost(#alpha={:.2f})'.format(ur) for ur in uboost_urs]
    uboost_pattern = 'uboost_05TotStat_ur_{{:4.2f}}_te_{:.0f}_WTopnote_hypsel_500est'.format(uboost_eff)

    '''
    nn_mass_var = "NN(m-weight)"
    nn_linear_vars = list()
    for lambda_reg_ in lambda_regs:
        nn_linear_var_ = "NN(rho,#lambda={:.0f})".format(lambda_reg_)
        nn_linear_vars.append(nn_linear_var_)
        pass
    nn_linear_var = nn_linear_vars[lambda_regs.index(lambda_reg)]
    '''

    # -- Tagger feature collection
    tagger_features = ['Tau21','Tau21DDT', 'D2', D2_kNN_var, 'D2CSS', 'NN', ann_var, 'Adaboost', uboost_var]


    # Adding variables
    # --------------------------------------------------------------------------
    with Profile("Adding variables"):

        # Tau21DDT
        # - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -
        with Profile("Tau21DDT"):
            from run.ddt.common import add_ddt
            add_ddt(data, path='models/ddt/ddt.pkl.gz')
            pass


        # D2-kNN
        # - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -
        with Profile("D2-kNN"):
            from run.knn.common import add_knn, VAR as knn_var
            add_knn(data, newfeat=D2_kNN_var, path='models/knn/knn_{}_{}.pkl.gz'.format(knn_var, kNN_eff))
            pass


        # D2-CSS
        # - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -
        with Profile("D2-CSS"):
            from run.css.common import AddCSS
            AddCSS("D2", data)
            pass


        # NN
        # - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -
        with Profile("NN"):
            classifier = load_model('models/adversarial/classifier/full/classifier.h5')

            data['NN'] = pd.Series(classifier.predict(data[features].as_matrix().astype(K.floatx()), batch_size=2048 * 8).flatten().astype(K.floatx()), index=data.index)
            pass


        # ANN
        # - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -
        with Profile("ANN"):
            from adversarial.utils import DECORRELATION_VARIABLES
            adversary = adversary_model(gmm_dimensions=len(DECORRELATION_VARIABLES),
                                        **cfg['adversary']['model'])

            combined = combined_model(classifier, adversary,
                                      **cfg['combined']['model'])

            for ann_var_, lambda_str_ in zip(ann_vars, lambda_strs):
                print "== Loading model for {}".format(ann_var_)
                combined.load_weights('models/adversarial/combined/full/combined_lambda{}.h5'.format(lambda_str_))
                data[ann_var_] = pd.Series(classifier.predict(data[features].as_matrix().astype(K.floatx()), batch_size=2048 * 8).flatten().astype(K.floatx()), index=data.index)
                pass
            print "== Done loading ANN models"
            pass


        # Adaboost/uBoost
        # - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -
        from sklearn.externals.joblib.parallel import cpu_count, Parallel, delayed

        def _predict(estimator, X, method, start, stop):
            return getattr(estimator, method)(X[start:stop])

        def parallel_predict(estimator, X, n_jobs=1, method='predict', batches_per_job=3):
            n_jobs = max(cpu_count() + 1 + n_jobs, 1)  # XXX: this should really be done by joblib
            n_batches = batches_per_job * n_jobs
            n_samples = len(X)
            batch_size = int(np.ceil(n_samples / n_batches))
            parallel = Parallel(n_jobs=n_jobs, backend="threading")
            results = parallel(delayed(_predict, check_pickle=False)(estimator, X, method, i, i + batch_size)
                               for i in range(0, n_samples, batch_size))
            return np.concatenate(results)

        with Profile("Adaboost/uBoost"):

            for var, ur in zip(uboost_vars, uboost_urs):
                print "== Loading model for {}".format(var)
                with gzip.open('models/uboost/' + uboost_pattern.format(ur).replace('.', 'p') + '.pkl.gz', 'r') as f:
                    clf = pickle.load(f)
                    pass

                # You parallelisation to speed-up prediction
                result = parallel_predict(clf, data, n_jobs=16, method='predict_proba')

                var = ('Adaboost' if ur == 0 else var)
                #data[var] =
                #pd.Series(clf.predict_proba(data)[:,1].flatten().astype(K.floatx()),
                #index=data.index)
                data[var] = pd.Series(result[:,1].flatten().astype(K.floatx()), index=data.index)
                pass

            # Remove `Adaboost` from scan list
            uboost_vars.pop(0)

            print "== Done loading Ababoost/uBoost models"
            pass
        pass


    # Perform pile-up robustness study
    # --------------------------------------------------------------------------
    with Profile("Study: Robustness (pile-up)"):
        bins = [0, 9.5, 13.5, 16.5, 20.5, 30.5]
        studies.robustness(data, args, tagger_features, 'EventInfo_NPV', bins)
        pass


    # Perform pT robustness study
    # --------------------------------------------------------------------------
    with Profile("Study: Robustness (pT)"):
        bins = [200, 260, 330, 430, 560, 720, 930, 1200, 1550, 2000]
        studies.robustness(data, args, tagger_features, 'pt', bins)
        pass


    # Perform jet mass distribution comparison study
    # --------------------------------------------------------------------------
    with Profile("Study: Jet mass comparison"):
        for simple_features in [True, False]:
            studies.jetmasscomparison(data, args, tagger_features, simple_features)
            pass
        pass


    # Perform summary plot study
    # --------------------------------------------------------------------------
    with Profile("Study: Summary plot"):
        regex_nn = re.compile('\#lambda=[\d\.]+')
        regex_ub = re.compile('\#alpha=[\d\.]+')

        scan_features = {'NN': map(lambda feat: (feat, regex_nn.search(feat).group(0)), ann_vars),
                         'Adaboost': map(lambda feat: (feat, regex_ub.search(feat).group(0)), uboost_vars)}

        studies.summary(data, args, tagger_features, scan_features)
        pass


    # Perform distributions study
    # --------------------------------------------------------------------------
    with Profile("Study: Substructure tagger distributions"):
        for feat in tagger_features:
            studies.distribution(data, args, feat)
            pass
        pass


    # Perform jet mass distributions study
    # --------------------------------------------------------------------------
    with Profile("Study: Jet mass distributions"):
        for feat in tagger_features:
            studies.jetmass(data, args, feat)
            pass
        pass


    # Perform ROC study
    # --------------------------------------------------------------------------
    with Profile("Study: ROC"):
        studies.roc(data, args, tagger_features)
        pass


    # Perform JSD study
    # --------------------------------------------------------------------------
    with Profile("Study: JSD"):
        studies.jsd(data, args, tagger_features)
        pass

    # Perform efficiency study
    # --------------------------------------------------------------------------
    with Profile("Study: Efficiency"):
        for feat in tagger_features:
            studies.efficiency(data, args, feat)
            pass
        pass

    return 0


# Main function call
if __name__ == '__main__':

    # Parse command-line arguments
    args = parse_args(backend=True, plots=True)

    # Call main function
    main(args)
    pass
