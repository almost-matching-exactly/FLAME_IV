# coding: utf-8

# In[1]:

import numpy as np
import pandas as pd
import pyodbc
import time
import pickle
import operator
from operator import itemgetter
from joblib import Parallel, delayed

from sklearn import linear_model
import statsmodels.formula.api as sm
from sklearn.linear_model import Ridge
from sklearn.tree import DecisionTreeRegressor
from sklearn.model_selection import cross_val_score
from sklearn import linear_model
from sklearn.metrics import mean_squared_error
from sqlalchemy import create_engine

import psycopg2
from sklearn.utils import shuffle

import sql
from sklearn import feature_selection

from sklearn import linear_model
import statsmodels.formula.api as sm
from statsmodels.stats import anova
import pylab as pl

import warnings
from sqlalchemy.pool import NullPool
from multiprocessing import Pool
from functools import partial
from pysal.spreg.twosls import TSLS
from decimal import *
from statsmodels import robust
from astropy.stats import median_absolute_deviation
import math

def construct_sec_order(arr): 
    second_order_feature = []
    num_cov_sec = len(arr[0])
    for a in arr:
        tmp = []
        for i in range(num_cov_sec):
            for j in range(i+1, num_cov_sec):
                tmp.append( a[i] * a[j] )
        second_order_feature.append(tmp)
        
    return np.array(second_order_feature)

def data_generation_dense_2(covariance, num_control, num_treated, num_cov_important, num_cov_unimportant, pi, control_m = 0.1, treated_m = 0.9):

    def gen_xz(num_treated, num_control, dim):
        z = np.concatenate((np.zeros(num_control), np.ones(num_treated)), axis = 0).reshape((num_treated + num_control, 1))

        x1_0 = np.random.binomial(1,0.5,size = (num_control,num_cov_important))
        x1_1 = np.random.binomial(1,0.1,size = (num_control,num_cov_unimportant))
        x1 = np.hstack((x1_0,x1_1))

        x2_0 = np.random.binomial(1,0.5,size = (num_treated,num_cov_important))
        x2_1 = np.random.binomial(1,0.9,size = (num_treated,num_cov_unimportant))
        x2 = np.hstack((x2_0,x2_1))

        x = np.concatenate((x1, x2), axis = 0)

        return x, z
    
    def get_gamma(num_cov, ratio, base):
        gamma = []
        for i in range(num_cov):
            gamma.append(base)
            base = base * ratio
        return gamma
            
    #parameters
    alpha = 0
    k = 0
    beta = 10   
    dim = num_cov_important + num_cov_unimportant
    mean_rou = 0.1
    rou = np.random.normal(mean_rou, mean_rou / 10, dim).reshape((dim,1))
    epsilon_ksi = np.random.multivariate_normal([0,0], [[1, covariance], [covariance, 1]], num_control + num_treated)
    
    x,z = gen_xz(num_treated, num_control, dim)
    xz = np.concatenate((x, z), axis =1)

    dij = np.add(pi * z, np.matmul(x, rou))
    dij = np.add(dij, epsilon_ksi[:,1].reshape((num_treated + num_control,1)))
    
    threshold1 = 0.3
    threshold2 = 0.6
    threshold3 = 1.0
    Dij = np.asarray([0 if e < threshold1 else 1 if e < threshold2 else 2 if e < threshold3 else 3 for e in dij[:,0]]).reshape((num_treated + num_control,1))

    gamma = get_gamma(dim,0.5,5)
    Rij = np.add(beta * Dij, np.matmul(x, gamma).reshape((num_treated + num_control,1)))
    second_order = 10 * construct_sec_order(x[:,:5]).sum(axis = 1)
    Rij = np.add(Rij,second_order.reshape((num_treated + num_control,1)))
    Rij = np.add(Rij, epsilon_ksi[:,0].reshape([num_treated + num_control,1]))     

    df = pd.DataFrame(np.concatenate([x, z, Dij, Rij], axis = 1))
    df.columns = df.columns.astype(str)
    start_index = dim
    df.rename(columns = {str(start_index):"iv"}, inplace = True)
    df['iv'] = df['iv'].astype('int64')  
    df.rename(columns = {str(start_index+1):"treated"}, inplace = True)
    df['treated'] = df['treated'].astype('int64') 
    df.rename(columns = {str(start_index+2):"outcome"}, inplace = True)

    df['zr'] = df['iv'] * df['outcome']
    df['zd'] = df['iv'] * df['treated']
    df['matched'] = 0


    df.reset_index()
    df['index'] = df.index

    return df,x,z,Dij,Rij

# this function takes the current covariate list, the covariate we consider dropping, name of the data table, 
# name of the holdout table, the threshold (below which we consider as no match), and balancing regularization
# as input; and outputs the matching quality
def score_tentative_drop_c(cov_l, c, db_name, holdout_df, thres = 0, tradeoff = 0.1):
    conn = psycopg2.connect("dbname='postgres' user='postgres' host='localhost' password='yaoyj11 '")
    cur = conn.cursor() 
    
    covs_to_match_on = set(cov_l) - {c} # the covariates to match on
    
    # the flowing query fetches the matched results (the variates, the outcome, the treatment indicator)
    s = time.time()
    
    cur.execute('''with temp AS 
        (SELECT 
        {0}
        FROM {3}
        where "matched"=0
        group by {0}
        Having sum("iv") > 0 and sum("iv") < count(*)
        )
        (SELECT {1}, iv,treated, outcome
        FROM {3}
        WHERE "matched"=0 AND EXISTS 
        (SELECT 1
        FROM temp 
        WHERE {2}
        )
        )
        '''.format(','.join(['"{0}"'.format(v) for v in covs_to_match_on ]),
                   ','.join(['{1}."{0}"'.format(v, db_name) for v in covs_to_match_on ]),
                   ' AND '.join([ '{1}."{0}"=temp."{0}"'.format(v, db_name) for v in covs_to_match_on ]),
                   db_name
                  ) )
    res = np.array(cur.fetchall())
    
    time_match = time.time() - s
    
    s = time.time()
    # the number of unmatched treated units
    cur.execute('''select count(*) from {} where "matched" = 0 and "iv" = 0'''.format(db_name))
    num_control = cur.fetchall()
    # the number of unmatched control units
    cur.execute('''select count(*) from {} where "matched" = 0 and "iv" = 1'''.format(db_name))
    num_treated = cur.fetchall()
    time_BF = time.time() - s
    
    s = time.time() # the time for fetching data into memory is not counted if use this
    
    tree_c = Ridge(alpha = 0.1)
    tree_t = Ridge(alpha = 0.1)
    
    holdout = holdout_df.copy()
    holdout = holdout[ ["{}".format(c) for c in covs_to_match_on] + ['iv', 'treated', 'outcome']]

    mse_t = np.mean(cross_val_score(tree_t, holdout[holdout['iv'] == 1].iloc[:,:-3], 
                                holdout[holdout['iv'] == 1]['outcome'] , scoring = 'neg_mean_squared_error' ) )
        
    mse_c = np.mean(cross_val_score(tree_c, holdout[holdout['iv'] == 0].iloc[:,:-3], 
                                holdout[holdout['iv'] == 0]['outcome'], scoring = 'neg_mean_squared_error' ) )
      
    time_PE = time.time() - s
    
    if len(res) == 0:
        return (( mse_t + mse_c ), time_match, time_PE, time_BF)
    else:        
        return (tradeoff * (float(len(res[res[:,-3]==0]))/num_control[0][0] + float(len(res[res[:,-3]==1]))/num_treated[0][0]) +             ( mse_t + mse_c ), time_match, time_PE, time_BF)
        
# update matched units
# this function takes the currcent set of covariates and the name of the database; and update the "matched"
# column of the newly mathced units to be "1"

def update_matched(cur, conn, covs_matched_on, db_name, level):  

    cur.execute('''with temp AS 
        (SELECT 
        {0}
        FROM {3}
        where "matched"=0
        group by {0}
        Having sum("iv") > 0 and sum("iv") < count(*)
        )
        update {3} set "matched"={4}
        WHERE EXISTS
        (SELECT {0}
        FROM temp
        WHERE {2} and {3}."matched" = 0
        )
        '''.format(','.join(['"{0}"'.format(v) for v in covs_matched_on]),
                   ','.join(['{1}."{0}"'.format(v, db_name) for v in covs_matched_on]),
                   ' AND '.join([ '{1}."{0}"=temp."{0}"'.format(v, db_name) for v in covs_matched_on ]),
                   db_name,
                   level
                  ) )

    conn.commit()

    return

# get CATEs 
# this function takes a list of covariates and the name of the data table as input and outputs a dataframe 
# containing the combination of covariate values and the corresponding CATE 
# and the corresponding effect (and the count and variance) as values

def get_CATE_db(cur, cov_l, db_name, level):
    cur.execute(''' select {0},count(*),sum(treated),sum(outcome),array_agg(index)
                    from {1}
                    where matched = {2} and iv = 0
                    group by {0}
                    '''.format(','.join(['"{0}"'.format(v) for v in cov_l]), 
                              db_name, level) )
    res_c = cur.fetchall()
       
    cur.execute(''' select {0},count(*),sum(treated),sum(outcome),array_agg(index)
                    from {1}
                    where matched = {2} and iv = 1
                    group by {0}
                    '''.format(','.join(['"{0}"'.format(v) for v in cov_l]), 
                              db_name, level) )
    res_t = cur.fetchall()
     
    if (len(res_c) == 0) | (len(res_t) == 0):
        return None

    cov_l = list(cov_l)

    result = pd.merge(pd.DataFrame(np.array(res_c), columns=['{}'.format(i) for i in cov_l]+['count_0','sum_treated_0','sum_outcome_0', 'index_0' ]), 
                  pd.DataFrame(np.array(res_t), columns=['{}'.format(i) for i in cov_l]+['count_1','sum_treated_1','sum_outcome_1', 'index_1']), 
                  on = ['{}'.format(i) for i in cov_l], how = 'inner') 
    
   
    result_df = result[['{}'.format(i) for i in cov_l] + ['count_0','sum_treated_0','sum_outcome_0','index_0','count_1','sum_treated_1','sum_outcome_1', 'index_1']]
    
    if result_df is None or result_df.empty:
        return None
    
    result_df['count'] = result_df['count_0'] + result_df['count_1']
    result_df['sum_outcome_1'] = result_df['sum_outcome_1'].astype('float64')
    result_df['sum_outcome_0'] = result_df['sum_outcome_0'].astype('float64')
    result_df['sum_treated_1'] = result_df['sum_treated_1'].astype('float64')
    result_df['sum_treated_0'] = result_df['sum_treated_0'].astype('float64')
    result_df['CACE_y'] = result_df['sum_outcome_1'] * 1.0 / result_df['count_1'] - result_df['sum_outcome_0'] * 1.0 / result_df['count_0']
    result_df['CACE_t'] = result_df['sum_treated_1'] * 1.0 / result_df['count_1'] - result_df['sum_treated_0'] * 1.0 / result_df['count_0']
    result_df['index'] = result_df['index_0'] + result_df['index_1']
    index = ['count','CACE_y','CACE_t', 'index']

    result_df = result_df[index]

    sum_all = result_df.sum(axis = 0)
    print(sum_all['count'])

    return result_df


def run_db(cur, conn, db_name, holdout_df, num_covs, reg_param = 0.1):
    cur.execute('update {0} set matched = 0'.format(db_name)) # reset the matched indicator to 0
    conn.commit()

    covs_dropped = [] # covariate dropped
    ds = []
    score_list = []
    
    level = 1
    #print(level)

    cur_covs = range(num_covs) 
    
    update_matched(cur, conn, cur_covs, db_name, level) # match without dropping anything
    d = get_CATE_db(cur, cur_covs, db_name, level) # get CATE without dropping anything
    ds.append(d)
    
    while len(cur_covs)>1:
        level += 1
        #print(level)
        
        cur.execute('''select count(*) from {} where "matched"=0 and "iv"=0'''.format(db_name))
        if cur.fetchall()[0][0] == 0:
            break
        cur.execute('''select count(*) from {} where "matched"=0 and "iv"=1'''.format(db_name))
        if cur.fetchall()[0][0] == 0:
            break
        
        best_score = -np.inf
        cov_to_drop = None

        cur_covs = list(cur_covs)
        for c in cur_covs:
            score,time_match,time_PE,time_BF = score_tentative_drop_c(cur_covs, c, db_name, 
                                                                      holdout_df, tradeoff = 0.1)
            
            if score > best_score:
                best_score = score
                cov_to_drop = c
                
        cur_covs = set(cur_covs) - {cov_to_drop} # remove the dropped covariate from the current covariate set

        update_matched(cur, conn, cur_covs, db_name, level)
        score_list.append(best_score)
        d = get_CATE_db(cur, cur_covs, db_name, level)
        ds.append(d)
        covs_dropped.append(cov_to_drop) # append the removed covariate at the end of the covariate    
      
    return ds

def get_LATE(res):
    match_list = res
    index_list = ['count','CACE_y','CACE_t','index']

    df_all = pd.DataFrame(columns = index_list)

    for row in match_list:
        if row is None or row.empty:
            continue
        df = pd.DataFrame(row)
        df_all = pd.concat([df_all,df],axis = 0)
    
    ATE = None
    if not df_all.empty:
        df_all['weighted_CACE_y'] = df_all['CACE_y'] * df_all['count']
        df_all['weighted_CACE_t'] = df_all['CACE_t'] * df_all['count']
        sum_all = df_all.sum(axis = 0)
        ATE = sum_all['weighted_CACE_y']/sum_all['weighted_CACE_t']

    return ATE

def get_LATE_and_CI(df,res,total_num):
    print(total_num)
    match_list = res
    match_list = res
    index_list = ['count','CACE_y','CACE_t','index']

    df_all = pd.DataFrame(columns = index_list)

    for row in match_list:
        if row is None or row.empty:
            continue
        df_sub = pd.DataFrame(row)
        df_all = pd.concat([df_all,df_sub],axis = 0)
    
    ATE = None
    ITT_y = None
    ITT_t = None
    if not df_all.empty:
        df_all['weighted_CACE_y'] = df_all['CACE_y'] * df_all['count']
        df_all['weighted_CACE_t'] = df_all['CACE_t'] * df_all['count']
        sum_all = df_all.sum(axis = 0)
        ITT_y = sum_all['weighted_CACE_y']
        ITT_t = sum_all['weighted_CACE_t']
        ATE = ITT_y/ITT_t

    
    if ATE is None:
        return None, None, None

    var_y = 0
    var_t = 0
    cov = 0

    ###get CI
    for level in match_list:
        if level is None or level.empty:
            continue
        for idx, row in level.iterrows():
            idx_list = row['index']
            df_sub = df[df['index'].isin(idx_list)]
            n_1 = sum(df_sub['iv'].tolist())
            n_0 = df_sub.shape[1] - n_1
            if n_0 == 1 or n_1 == 1 or n_0 == 0 or n_1 == 0:
                continue
            df_sub_0 = df_sub[df_sub['iv'] == 0]
            df_sub_1 = df_sub[df_sub['iv'] == 1]
            y_z_0 = df_sub_0['outcome'] * df_sub_0['iv'].sum()
            y_z_1 = df_sub_1['outcome'] * df_sub_1['iv'].sum() 
            t_z_1 = df_sub_1['treated'] * df_sub_1['iv'].sum() 
            s_0 = ((df_sub['outcome'] * (1 - df_sub['iv']) - n_0 * y_z_0) ** 2).sum() * 1.0 / (n_0 - 1)
            s_1 = ((df_sub['outcome'] * df_sub['iv'] - n_1 * y_z_1) ** 2).sum() * 1.0 / (n_1 - 1)
            r_1 = ((df_sub['treated'] * df_sub['iv'] - n_1 * t_z_1) ** 2).sum() * 1.0 / (n_1 - 1)
            var_y += (s_1 / n_1 + s_0 / n_0) * ((n_1 + n_0) ** 2) / (total_num ** 2)
            var_t += (r_1 / n_1) * ((n_1 + n_0) ** 2) / (total_num ** 2)
            cov_grp = ((df['outcome'] * df['iv'] - y_z_1 * 1.0 / n_1) * (df['treated'] * df['iv'] - t_z_1 * 1.0 / n_1)).sum() / (n_1 * (n_1 - 1))
            cov += cov_grp * ((n_1 + n_0) ** 2) / (total_num ** 2)
    
    sigma = var_y / (ITT_t ** 2) + (ITT_y ** 2) * var_t / (ITT_t ** 4) - 2 * ITT_y * cov / (ITT_t ** 3)
    std = math.sqrt(sigma)

    return ATE, ATE - 1.96 * std, ATE + 1.96 * std

def run(pi):
    conn = psycopg2.connect("dbname='postgres' user='postgres' host='localhost' password='yaoyj11 '")
    cur = conn.cursor()  
    engine = create_engine('postgresql+psycopg2://postgres:yaoyj11 @localhost/postgres', poolclass=NullPool)
    table_name = 'flame_' + str(int(100*pi))
    
    LATE_list = []  
    
    df_all= pickle.load(open('data/df_nonlinear_15000_multilevel_'+str(pi), "rb"))

    np.random.seed(10)
    for i in range(1):
        cur.execute('drop table if exists {}'.format(table_name))
        conn.commit()
        cov = 0.8
        df = df_all[i]
        holdout_df,x,z,d,r = data_generation_dense_2(cov,1000,1000,8,2,pi)  
        df.to_sql(table_name, engine)
        res = run_db(cur, conn,table_name, holdout_df, 10)
        get_LATE_and_CI(df,res,df.shape[0])
        

if __name__ == '__main__':
    def warn(*args, **kwargs):
        pass
    warnings.warn = warn

    mean_bias = []
    median_bias = []
    mean_deviation = []
    median_deviation = []

    #pi_array = [0.1,0.2,0.3,0.4,0.5,0.6,0.7,0.8]
    pi_array = [0.5]
    
    with Pool(min(10, len(pi_array))) as p:
        drop_results = p.map(run, pi_array)
        