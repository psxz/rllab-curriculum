"""
Check whether par-TRPO computes state count correctly
"""
# imports -----------------------------------------------------
""" baseline """
from sandbox.haoran.parallel_trpo.linear_feature_baseline import ParallelLinearFeatureBaseline
from rllab.baselines.linear_feature_baseline import LinearFeatureBaseline

""" policy """
from rllab.policies.categorical_mlp_policy import CategoricalMLPPolicy

""" optimizer """
from sandbox.haoran.parallel_trpo.conjugate_gradient_optimizer import ParallelConjugateGradientOptimizer
from rllab.optimizers.conjugate_gradient_optimizer import ConjugateGradientOptimizer

""" algorithm """
from sandbox.haoran.parallel_trpo.trpo import ParallelTRPO
from sandbox.haoran.hashing.bonus_trpo.algos.bonus_trpo_theano import BonusTRPO

""" environment """
from sandbox.haoran.hashing.bonus_trpo.envs.atari_env import AtariEnv

""" resetter """
from sandbox.haoran.hashing.bonus_trpo.resetter.atari_save_load_resetter import AtariSaveLoadResetter

""" bonus """
from sandbox.haoran.hashing.bonus_trpo.bonus_evaluators.ale_hashing_bonus_evaluator import ALEHashingBonusEvaluator
from sandbox.haoran.hashing.bonus_trpo.bonus_evaluators.zero_bonus_evaluator import ZeroBonusEvaluator
from sandbox.haoran.hashing.bonus_trpo.bonus_evaluators.preprocessor.identity_preprocessor import IdentityPreprocessor
from sandbox.haoran.hashing.bonus_trpo.bonus_evaluators.hash.sim_hash import SimHash
from sandbox.haoran.hashing.bonus_trpo.bonus_evaluators.hash.sim_hash_v2 import SimHashV2
from sandbox.haoran.hashing.bonus_trpo.bonus_evaluators.preprocessor.image_vectorize_preprocessor import ImageVectorizePreprocessor

""" others """
from sandbox.haoran.myscripts.myutilities import get_time_stamp
from sandbox.haoran.ec2_info import instance_info, subnet_info
from rllab import config
from rllab.misc.instrument import stub, run_experiment_lite
import sys,os
import copy

stub(globals())

from rllab.misc.instrument import VariantGenerator, variant

# exp setup -----------------------------------------------------
exp_index = os.path.basename(__file__).split('.')[0] # exp_xxx
exp_prefix = "bonus-trpo-atari/" + exp_index
mode = "local"
ec2_instance = "c4.8xlarge"
subnet = "us-west-1a"
config.DOCKER_IMAGE = "tsukuyomi2044/rllab3" # needs psutils

n_parallel = 8 # only for local exp
snapshot_mode = "last"
plot = False
use_gpu = False
sync_s3_pkl = True
config.USE_TF = False

if "local" in mode and sys.platform == "darwin":
    set_cpu_affinity = False
    cpu_assignments = None
    serial_compile = False
else:
    set_cpu_affinity = True
    cpu_assignments = None
    serial_compile = True

# variant params ---------------------------------------
class VG(VariantGenerator):
    @variant
    def seed(self):
        return [0]

    @variant
    def bonus_coeff(self):
        return [0.01]

    @variant
    def game(self):
        return ["montezuma_revenge"]

    @variant
    def dim_key(self):
        return [64]

    @variant
    def count_target(self):
        return ["images"]

    @variant
    def entropy_bonus(self): # only for ParalellTRPO
        return [1e-2]

    @variant
    def use_parallel(self):
        return [True, False]
variants = VG().variants()


print("#Experiments: %d" % len(variants))
for v in variants:
    # non-variant params ------------------------------
    # algo
    use_parallel = v["use_parallel"]
    seed=v["seed"]
    if mode == "local_test":
        batch_size = 50
    else:
        batch_size = 50
    max_path_length = 4500
    discount = 0.99
    n_itr = 10
    step_size = 0.01
    policy_opt_args = dict(
        name="pi_opt",
        cg_iters=10,
        reg_coeff=1e-5,
        subsample_factor=1.,
        max_backtracks=15,
        backtrack_ratio=0.8,
        accept_violation=False,
        hvp_approach=None,
        num_slices=1, # reduces memory requirement
    )
    entropy_bonus = v["entropy_bonus"]

    # env
    game=v["game"]
    env_seed=1 # deterministic env
    frame_skip=4
    img_width=84
    img_height=84
    n_last_screens=1
    clip_reward = True
    obs_type = "ram"
    count_target = v["count_target"]
    record_image=(count_target == "images")
    record_rgb_image=False
    record_ram=(count_target == "ram_states")
    record_internal_state=False

    # bonus
    bonus_coeff=v["bonus_coeff"]
    bonus_form="1/sqrt(n)"
    count_target=v["count_target"]
    retrieve_sample_size=100000 # compute keys for all paths at once
    bucket_sizes = None

    # others
    baseline_prediction_clip = 100

    # other exp setup --------------------------------------
    exp_name = "{exp_index}_{time}_{game}".format(
        exp_index=exp_index,
        time=get_time_stamp(),
        game=game,
    )
    if ("ec2" in mode) and (len(exp_name) > 64):
        print("Should not use experiment name with length %d > 64.\nThe experiment name is %s.\n Exit now."%(len(exp_name),exp_name))
        sys.exit(1)

    if use_gpu:
        config.USE_GPU = True
        config.DOCKER_IMAGE = "dementrock/rllab3-shared-gpu"

    if "local_docker" in mode:
        actual_mode = "local_docker"
    elif "local" in mode:
        actual_mode = "local"
    elif "ec2" in mode:
        actual_mode = "ec2"
        # configure instance
        info = instance_info[ec2_instance]
        config.AWS_INSTANCE_TYPE = ec2_instance
        config.AWS_SPOT_PRICE = str(info["price"])
        n_parallel = int(info["vCPU"] /2)

        # choose subnet
        config.AWS_NETWORK_INTERFACES = [
            dict(
                SubnetId=subnet_info[subnet]["SubnetID"],
                Groups=subnet_info[subnet]["Groups"],
                DeviceIndex=0,
                AssociatePublicIpAddress=True,
            )
        ]
    elif "kube" in mode:
        actual_mode = "lab_kube"
        info = instance_info[ec2_instance]
        n_parallel = int(info["vCPU"] /2)

        config.KUBE_DEFAULT_RESOURCES = {
            "requests": {
                "cpu": n_parallel
            }
        }
        config.KUBE_DEFAULT_NODE_SELECTOR = {
            "aws/type": ec2_instance
        }
        exp_prefix = exp_prefix.replace('/','-') # otherwise kube rejects
    else:
        raise NotImplementedError

    # construct objects ----------------------------------
    env = AtariEnv(
        game=game,
        seed=env_seed,
        img_width=img_width,
        img_height=img_height,
        obs_type=obs_type,
        record_ram=record_ram,
        record_image=record_image,
        record_rgb_image=record_rgb_image,
        record_internal_state=record_internal_state,
        frame_skip=frame_skip,
    )
    policy = CategoricalMLPPolicy(
        env_spec=env.spec,
        hidden_sizes=(32,32),
    )


    # bonus
    if count_target == "images":
        state_preprocessor = ImageVectorizePreprocessor(
            n_channel=n_last_screens,
            width=img_width,
            height=img_height,
        )
    elif count_target == "ram_states":
        state_preprocessor = ImageVectorizePreprocessor(
            n_channel=1,
            width=128,
            height=1,
        )
    else:
        raise NotImplementedError

    _hash = SimHashV2(
        item_dim=state_preprocessor.get_output_dim(), # get around stub
        dim_key=v["dim_key"],
        bucket_sizes=bucket_sizes,
        parallel=use_parallel,
    )
    bonus_evaluator = ALEHashingBonusEvaluator(
        log_prefix="",
        state_dim=state_preprocessor.get_output_dim(), # get around stub
        state_preprocessor=state_preprocessor,
        hash=_hash,
        bonus_form=bonus_form,
        count_target=count_target,
        parallel=use_parallel,
        retrieve_sample_size=retrieve_sample_size,
    )

    if use_parallel:
        # baseline
        baseline = ParallelLinearFeatureBaseline(env_spec=env.spec)
        algo = ParallelTRPO(
            env=env,
            policy=policy,
            baseline=baseline,
            bonus_evaluator=bonus_evaluator,
            bonus_coeff=bonus_coeff,
            batch_size=batch_size,
            max_path_length=max_path_length,
            discount=discount,
            n_itr=n_itr,
            clip_reward=clip_reward,
            plot=plot,
            optimizer_args=policy_opt_args,
            step_size=step_size,
            set_cpu_affinity=set_cpu_affinity,
            cpu_assignments=cpu_assignments,
            serial_compile=serial_compile,
            n_parallel=n_parallel,
            entropy_bonus=entropy_bonus,
        )

        run_experiment_lite(
            algo.train(),
            exp_prefix=exp_prefix,
            exp_name=exp_name,
            seed=seed,
            snapshot_mode=snapshot_mode,
            mode=actual_mode,
            variant=v,
            use_gpu=use_gpu,
            plot=plot,
            sync_s3_pkl=sync_s3_pkl,
            sync_log_on_termination=True,
            sync_all_data_node_to_s3=True,
        )
    else:
        baseline = LinearFeatureBaseline(env_spec=env.spec)
        policy_opt_args.pop("name")
        algo = BonusTRPO(
            env=env,
            policy=policy,
            baseline=baseline,
            bonus_evaluator=bonus_evaluator,
            extra_bonus_evaluator=ZeroBonusEvaluator(),
            bonus_coeff=bonus_coeff,
            batch_size=batch_size,
            max_path_length=max_path_length,
            discount=discount,
            n_itr=n_itr,
            clip_reward=clip_reward,
            plot=plot,
            optimizer_args=policy_opt_args,
            step_size=step_size,
        )

        run_experiment_lite(
            algo.train(),
            n_parallel=n_parallel,
            exp_prefix=exp_prefix,
            exp_name=exp_name,
            seed=seed,
            snapshot_mode=snapshot_mode,
            mode=actual_mode,
            variant=v,
            use_gpu=use_gpu,
            plot=plot,
            sync_s3_pkl=sync_s3_pkl,
            sync_log_on_termination=True,
            sync_all_data_node_to_s3=True,
        )

    if "test" in mode:
        sys.exit(0)

if ("local" not in mode) and ("test" not in mode):
    os.system("chmod 444 %s"%(__file__))
