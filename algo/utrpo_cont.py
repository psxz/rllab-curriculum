import numpy as np
import sys
from misc.console import SimpleMessage, prefix_log, tee_log, mkdir_p
from misc.tensor_utils import flatten_tensors
import multiprocessing
import cgtcompat as theano#theano
import cgtcompat.tensor as T#theano.tensor as T
import pydoc
from remote_sampler import RemoteSampler
import time
import itertools
import re
from lbfgs import lbfgs

def new_surrogate_obj(
        policy, input_var, Q_est_var, old_pdep_vars, action_var,
        lambda_var):
    pdep_vars = policy.pdep_vars
    # to compute KL, we actually need the mean and std of the old distribution...
    # what would be a good way to generalize that?
    kl = policy.kl(old_pdep_vars, pdep_vars)
    lr = policy.likelihood_ratio(old_pdep_vars, pdep_vars, action_var)
    mean_kl = T.mean(kl)
    # formulate as a minimization problem
    surrogate_loss = - T.mean(lr * Q_est_var)
    surrogate_obj = surrogate_loss + lambda_var * mean_kl
    return surrogate_obj, surrogate_loss, mean_kl


# Unconstrained TRPO
class UTRPOCont(object):

    def __init__(
            self, n_itr=500, max_samples_per_itr=50000,
            discount=0.98, stepsize=0.015,#1,#015,
            initial_lambda=0.5, max_opt_itr=20, exp_name='utrpo',
            n_parallel=multiprocessing.cpu_count(), adapt_lambda=True,
            reuse_lambda=True, sampler_module='algo.rollout_sampler',
            resume_file=None, optimizer_module='scipy.optimize.fmin_l_bfgs_b'):
        self.n_itr = n_itr
        self.max_samples_per_itr = max_samples_per_itr
        self.discount = discount
        self.stepsize = stepsize
        self.initial_lambda = initial_lambda
        self.max_opt_itr = max_opt_itr
        self.adapt_lambda = adapt_lambda
        self.n_parallel = n_parallel
        self.exp_name = exp_name
        # whether to start from the currently adapted lambda on the next
        # iteration
        self.reuse_lambda = reuse_lambda
        self.sampler_module = sampler_module
        self.optimizer_module = optimizer_module
        self.resume_file = None

    def train(self, gen_mdp, gen_policy):

        exp_timestamp = time.strftime("%Y%m%d%H%M%S")
        mdp = gen_mdp()
        policy = gen_policy(mdp)

        input_var = policy.input_var

        Q_est_var = T.vector('Q_est')  # N
        old_pdep_vars = [T.matrix('old_pdep_%d' % i) for i in range(len(policy.pdep_vars))]
        action_var = T.matrix('action')
        lambda_var = T.scalar('lambda')

        surrogate_obj, surrogate_loss, mean_kl = \
            new_surrogate_obj(
                policy, input_var, Q_est_var, old_pdep_vars, action_var,
                lambda_var)

        grads = theano.gradient.grad(surrogate_obj, policy.params)

        all_inputs = [input_var, Q_est_var] + old_pdep_vars + [action_var, lambda_var]

        exp_logger = prefix_log('[%s] | ' % (self.exp_name))

        with SimpleMessage("Compiling functions...", exp_logger):
            compute_surrogate_kl = theano.function(
                all_inputs, [surrogate_obj, mean_kl], on_unused_input='ignore',
                allow_input_downcast=True
                )
            compute_mean_kl = theano.function(
                all_inputs, mean_kl, on_unused_input='ignore',
                allow_input_downcast=True
                )
            compute_grads = theano.function(
                all_inputs, grads, on_unused_input='ignore',
                allow_input_downcast=True
                )

        optimizer = pydoc.locate(self.optimizer_module)

        logger = tee_log(self.exp_name + '_' + exp_timestamp + '.log')

        savedir = 'data/%s_%s' % (self.exp_name, exp_timestamp)
        mkdir_p(savedir)

        lambda_ = self.initial_lambda

        with RemoteSampler(
                self.sampler_module, self.n_parallel, gen_mdp,
                gen_policy, savedir) as sampler:

            if self.resume_file is not None:
                print 'Resuming from snapshot %s...' % self.resume_file
                resume_data = np.load(self.resume_file)
                start_itr = int(re.search('itr_(\d+)', self.resume_file).group(1)) + 1
                policy.set_param_values(resume_data['opt_policy_params'])
            else:
                start_itr = 0


            for itr in xrange(start_itr, self.n_itr):

                itr_log = prefix_log('[%s] itr #%d | ' % (self.exp_name, itr), logger=logger)

                cur_params = policy.get_param_values()

                itr_log('collecting samples...')

                tot_rewards, n_traj, all_obs, Q_est, all_pdeps, all_actions = \
                    sampler.request_samples(
                        itr, cur_params, self.max_samples_per_itr,
                        self.discount)

                Q_est = Q_est - np.mean(Q_est)
                Q_est = Q_est / (Q_est.std() + 1e-8)

                all_input_values = [all_obs, Q_est] + all_pdeps + [all_actions]

                to_save = {
                    'all_obs': all_obs,
                    'Q_est': Q_est,
                    'actions': all_actions,
                    'cur_policy_params': cur_params,
                }
                for idx, pdep in enumerate(all_pdeps):
                    to_save['pdep_%d' % idx] = pdep
                np.savez_compressed('check.npz', **to_save)

                import sys
                sys.exit()


                def evaluate_cost(lambda_):
                    def evaluate(params):
                        policy.set_param_values(params)
                        inputs_with_lambda = all_input_values + [lambda_]
                        val, mean_kl = compute_surrogate_kl(*inputs_with_lambda)
                        #print mean_kl
                        #if mean_kl > self.stepsize or not np.isfinite(val):
                        #    return 1e2#8#4#10
                        return val.astype(np.float64)
                    return evaluate

                def evaluate_grad(lambda_):
                    def evaluate(params):
                        policy.set_param_values(params)
                        grad = compute_grads(*(all_input_values + [lambda_]))
                        flattened_grad = flatten_tensors(map(np.asarray, grad))
                        #import ipdb; ipdb.set_trace()
                        return flattened_grad.astype(np.float64)
                    return evaluate

                avg_reward = tot_rewards * 1.0 / n_traj

                ent = policy.compute_entropy(all_pdeps)

                itr_log('entropy: %f' % ent)
                itr_log('perplexity: %f' % np.exp(ent))


                itr_log('avg reward: %f over %d trajectories' %
                        (avg_reward, n_traj))

                loss_before = evaluate_cost(0)(cur_params)
                itr_log('loss before: %f' % loss_before)

                if not self.reuse_lambda:
                    lambda_ = self.initial_lambda
                else:
                    lambda_ = min(10000, max(0.01, lambda_))

                with SimpleMessage('trying lambda=%.3f...' % lambda_, itr_log):
                    result = optimizer(
                        func=evaluate_cost(lambda_), x0=cur_params,
                        fprime=evaluate_grad(lambda_),
                        maxiter=self.max_opt_itr
                        )
                    loss, mean_kl = compute_surrogate_kl(*(all_input_values + [lambda_]))
                    itr_log('lambda %f => loss %f, mean kl %f' % (lambda_, loss, mean_kl))
                    opt_params = policy.get_param_values()
                # do line search on lambda
                if self.adapt_lambda:
                    max_search = 10
                    if itr - start_itr < 2:
                        max_search = 10
                    if mean_kl > self.stepsize:
                        for _ in xrange(max_search):
                            lambda_ = lambda_ * 2
                            with SimpleMessage('trying lambda=%f...' % lambda_, itr_log):
                                result = optimizer(
                                    func=evaluate_cost(lambda_), x0=cur_params,
                                    fprime=evaluate_grad(lambda_),
                                    maxiter=self.max_opt_itr)
                                inputs_with_lambda = all_input_values + [lambda_]
                                val, mean_kl = compute_surrogate_kl(*inputs_with_lambda)
                                itr_log('lambda %f => loss %f, mean kl %f' % (lambda_, val, mean_kl))
                            if np.isnan(mean_kl):
                                import ipdb
                                ipdb.set_trace()
                            opt_params = policy.get_param_values()
                            if mean_kl <= self.stepsize:
                                break
                    else:
                        for _ in xrange(max_search):
                            try_lambda_ = lambda_ * 0.5
                            with SimpleMessage('trying lambda=%f...' % try_lambda_, itr_log):
                                try_result = optimizer(
                                    func=evaluate_cost(try_lambda_), x0=cur_params,
                                    fprime=evaluate_grad(try_lambda_),
                                    maxiter=self.max_opt_itr)
                                inputs_with_lambda = all_input_values + [try_lambda_]
                                try_val, try_mean_kl = compute_surrogate_kl(*inputs_with_lambda)
                                itr_log('lambda %f => loss %f, mean kl %f' % (try_lambda_, try_val, try_mean_kl))
                            if np.isnan(mean_kl):
                                import ipdb
                                ipdb.set_trace()
                            if try_mean_kl > self.stepsize:
                                break
                            result = try_result
                            lambda_ = try_lambda_
                            mean_kl = try_mean_kl
                            opt_params = policy.get_param_values()

                #print 'new log std values: ', policy.log_std_var.get_value()
                policy.print_debug()

                policy.set_param_values(opt_params)
                loss_after = evaluate_cost(0)(policy.get_param_values())
                itr_log('optimization finished. loss after: %f. mean kl: %f. dloss: %f' % (loss_after, mean_kl, loss_before - loss_after))
                timestamp = time.strftime("%Y%m%d%H%M%S")
                with SimpleMessage("saving result...", exp_logger):
                    to_save = {
                        'cur_policy_params': cur_params,
                        'opt_policy_params': policy.get_param_values(),
                        'all_obs': all_obs,
                        'Q_est': Q_est,
                        'itr': itr,
                        'lambda': lambda_,
                        'loss': loss_after,
                        'mean_kl': mean_kl,
                        'actions': all_actions,
                    }
                    for idx, pdep in enumerate(all_pdeps):
                        to_save['pdep_%d' % idx] = pdep
                    np.savez_compressed('%s/itr_%d_%s.npz' % (savedir, itr, timestamp), **to_save)
                sys.stdout.flush()
