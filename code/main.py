import itertools
import logging
from collections import OrderedDict, defaultdict
from pathlib import Path
from typing import List

import hydra
import numpy as np
import pandas as pd
import torch
import wandb
from hydra.core.hydra_config import HydraConfig
from matplotlib import pyplot as plt
from omegaconf import DictConfig, OmegaConf
from tqdm import tqdm

from loader.dataset import TsDataset, BoostrapEnsembleTsDataset
from loader.generator import DataGenerator, ChronoSplittedTsDataset
from models.forcast.forcast_base import FCPredictionData
from models.forcast.forcast_service import ForcastService
from models.uncertainty.uc_service import UncertaintyService
from models.uncertainty.pi_base import PIPredictionStepData, PIModelPrediction, PICalibArtifacts
from utils.utils import set_seed

LOGGER = logging.getLogger(__name__)


SAVE_LOAD_UC_MODEL = False  # Not working atm


@hydra.main(version_base=None, config_path='./../configuration', config_name='default_config.yaml')
def my_app(cfg: DictConfig):
    cfg = cfg.config
    _setup(cfg)
    fc_persist_dir = f"{cfg.experiment_data.model_dir}/fc"
    uc_persist_dir = f"{cfg.experiment_data.model_dir}/uc"

    datasets = DataGenerator.get_data(cfg.dataset, cfg.task, replace_base_dir=cfg.experiment_data.data_dir)
    alphas = [cfg.task.alpha] if isinstance(cfg.task.alpha, float) else cfg.task.alpha
    if cfg.dataset.add_config is not None and 'data_subset' in cfg.dataset.add_config:
        # 1 indexed!
        first, last_included = cfg.dataset.add_config['data_subset'][0], cfg.dataset.add_config['data_subset'][1]
        datasets = datasets[first-1:last_included]

    # Prepare (Create, Train,..) underlying forcast models
    fc_service = _init_fc(cfg, datasets, fc_persist_dir)
    datasets = fc_service.prepare(datasets, alphas)

    # Calibrate/Train UC
    uc_service = _init_uc(cfg, fc_service, datasets, uc_persist_dir,
                          record_attention=cfg.evaluation['att_plot_vega'] or cfg.evaluation['att_hist_matplot'])
    uc_service.prepare(datasets, alphas, experiment_config=cfg.experiment_data, calib_trainer_config=cfg.trainer)

    # Evaluate
    if cfg.experiment_data.evaluate:
        Evaluator.evaluate(uc_service, datasets, alphas, cfg.evaluation)
    else:
        model = cfg.model_uc._target_.split(".")[-1]
        if model in ['EnbPIModel']:
            Evaluator.evaluate_sota_on_validation(uc_service, datasets, alphas, no_calib=True)
        elif model in ['AdaptiveCI', 'NexCP', 'SPICModel', 'EpsSelectionPIStat']:
            Evaluator.evaluate_sota_on_validation(uc_service, datasets, alphas, no_calib=False)
        else:
            LOGGER.info("Skip Evaluation")
    wandb.finish()


def _setup(config):
    config.experiment_data.experiment_dir = Path().cwd()
    set_seed(config.experiment_data.seed)
    LOGGER.info('Starting wandb.')
    exp_data = config.experiment_data
    wandb.Table.MAX_ARTIFACT_ROWS = 2000000
    wandb.Table.MAX_ROWS = 2000000
    if hasattr(exp_data, "offline"):
        if isinstance(exp_data.offline, bool):
            mode = 'offline' if exp_data.offline else 'online'
        else:
            mode = exp_data.offline
    else:
        mode = 'online'
    wandb.init(project=exp_data.project_name, name=HydraConfig.get().job.name, #dir=Path.cwd(),
               entity=exp_data.project_entity if hasattr(exp_data, "project_entity") else None, # Backward Compatible if attr not existing
               config=OmegaConf.to_container(config, resolve=True, throw_on_missing=True), mode=mode,
               tags=config.wandb.tags, notes=config.wandb.notes, group=config.wandb.group)


def _init_fc(config, datasets, fc_persist_dir) -> ForcastService:
    LOGGER.info('Initialize forcast service.')
    return ForcastService(lambda: hydra.utils.instantiate(config.model_fc, no_x_features=datasets[0].no_x_features,
                                                          alpha=config.task.alpha),
                          data_config=config.dataset, task_config=config.task, model_config=config.model_fc,
                          persist_dir=fc_persist_dir)


def _init_uc(config, fc_service, datasets, uc_persist_dir, record_attention) -> UncertaintyService:
    LOGGER.info('Initialize uncertainty service.')
    return UncertaintyService(lambda: hydra.utils.instantiate(config.model_uc, no_x_features=datasets[0].no_x_features,
                                                              alpha=config.task.alpha,
                                                              ts_ids=[ts.ts_id for ts in datasets],
                                                              record_attention=record_attention),
                              fc_service=fc_service, save_uc_models=SAVE_LOAD_UC_MODEL, data_config=config.dataset,
                              task_config=config.task, persist_dir=uc_persist_dir)

class Evaluator:

    @staticmethod
    def evaluate(uc_service, datasets, alphas, eval_config):
        overall_metrics_per_alpha = defaultdict(lambda: defaultdict(list))
        rolling_dfs = defaultdict(list)
        registered_overall = None
        for run_no, (dataset, alpha) in enumerate(itertools.product(datasets, alphas)):
            registered_overall, registered_per_ts = Evaluator._setup_metrics(dataset)
            LOGGER.info(f"Evaluation Number {run_no}: Start evaluation for TS {dataset.ts_id} with alpha {alpha}")
            prediction_log, prediction_log_artifacts, eps = Evaluator._evaluate_single_ts(
                uc_service, dataset, alpha, other_datasets=[d for d in datasets if d.ts_id != dataset.ts_id]
            )
            calib_artifact = uc_service.get_calib_artifact(dataset, alpha)
            metrics_per_alpha, roll_df = Evaluator._log_eval_finished(
                uc_service, prediction_log, prediction_log_artifacts, dataset, eps, alpha, calib_artifact,
                registered_per_ts, eval_config, is_first_dataset=run_no < len(alphas))
            if roll_df is not None:
                rolling_dfs[registered_per_ts[1]].append(roll_df)
            for metric, value in metrics_per_alpha.items():
                if metric in registered_overall[0]:
                    overall_metrics_per_alpha[alpha][metric].append(value)
        # Log Overall Metrics
        for alpha, metrics in overall_metrics_per_alpha.items():
            wandb.log({
                'alpha': alpha,
                **{f"{registered_overall[1]}{metric}": np.mean(values) for metric, values in metrics.items()}
            })
        # Log Rolling Config
        Evaluator.log_rolling_metrics(rolling_dfs, eval_config)

    @staticmethod
    def evaluate_sota_on_validation(uc_service, datasets, alphas, no_calib=False):
        overall_metrics = defaultdict(list)
        overall_metrics_per_alpha = defaultdict(lambda: defaultdict(list))
        wandb.define_metric(f"val/winkler_score_norm", summary='min', goal='minimize')
        for no_dataset, dataset in enumerate(datasets):
            # Make Calibration set to calib/val set
            calib_size = dataset.no_calib_steps
            test_size = dataset.no_test_steps
            val_calib = 0 if no_calib else calib_size // 2
            dataset._test_step = dataset.calib_step + val_calib
            # Remove Original Test Steps
            dataset._X = dataset._X[:-test_size]
            dataset._Y = dataset._Y[:-test_size]
            for no_alpha, alpha in enumerate(alphas):
                # registered_overall, registered_per_ts = Evaluator._setup_metrics(dataset)
                LOGGER.info(f"Evaluation on VALIDATION Number {no_dataset}/{no_alpha}: Start evaluation for TS {dataset.ts_id} with alpha {alpha}")
                #Evaluate
                prediction_log, prediction_log_artifacts, eps = Evaluator._evaluate_single_ts(
                    uc_service, dataset, alpha, other_datasets=[d for d in datasets if d.ts_id != dataset.ts_id]
                )
                metrics_per_alpha, _ = Evaluator._log_eval_finished(
                    uc_service, prediction_log, None, dataset, eps, alpha, None,
                    (dict(), 'dummy'), defaultdict(lambda: False), is_first_dataset=no_dataset == 0)
                overall_metrics['winkler_score_norm'].append(metrics_per_alpha['winkler_score_norm'])
                overall_metrics['mean_pi_width'].append(metrics_per_alpha['mean_pi_width'])
                overall_metrics_per_alpha[alpha]['covered'].append(int(len(prediction_log) * metrics_per_alpha['mean_coverage']))
                overall_metrics_per_alpha[alpha]['steps'].append(len(prediction_log))
        to_log = {}
        miss_coverages = []
        for alpha, metrics in overall_metrics_per_alpha.items():
            delta_coverage = (sum(metrics['covered']) / sum(metrics['steps'])) - (1 - alpha)
            miss_coverages.append(max(0, delta_coverage * -1.0))
            to_log[f'val/CoverageDiff_A_{alpha}'] = delta_coverage
        to_log[f'val/MissCoverage'] = sum(miss_coverages)
        to_log.update({**{f"val/{metric}": np.mean(values) for metric, values in overall_metrics.items()}})
        wandb.log(to_log)
        LOGGER.info(to_log)


    @staticmethod
    def _setup_metrics(dataset):
        """
        :return: 2 Tuple for "overall" and "per_ts" with:
                     1. Element: List of all registered metrics
                     2. Element: prefix string
        """
        prefix_overall = 'Eval/'
        prefix_per_ts = f"{prefix_overall[:-1]}_{dataset.ts_id}/"
        prefixes = defaultdict(list)

        # Overall and per TS Metrics
        for prefix in [prefix_overall, prefix_per_ts]:
            wandb.define_metric(f"{prefix}mean_coverage", step_metric='alpha', summary='none')
            prefixes[prefix].append("mean_coverage")
            wandb.define_metric(f"{prefix}mean_coverage_eps", step_metric='alpha', summary='mean', goal='minimize')
            prefixes[prefix].append('mean_coverage_eps')
            wandb.define_metric(f"{prefix}mean_pi_width", step_metric='alpha', summary='mean', goal='minimize')
            prefixes[prefix].append('mean_pi_width')
            wandb.define_metric(f"{prefix}mean_pi_sd", step_metric='alpha', summary='none')
            prefixes[prefix].append('mean_pi_sd')
            wandb.define_metric(f"{prefix}winkler_score", step_metric='alpha', summary='mean', goal='minimize')
            prefixes[prefix].append("winkler_score")
            wandb.define_metric(f"{prefix}winkler_score_norm", step_metric='alpha', summary='mean', goal='minimize')
            prefixes[prefix].append("winkler_score_norm")

        # Per TS metrics
        for prefix in [prefix_per_ts]:
            wandb.define_metric(f"{prefix}median_pi_with", step_metric='alpha', summary='mean', goal='minimize')
            #prefixes[prefix].append("median_pi_with")
            wandb.define_metric(f"{prefix}mean_dist_to_bound_in", step_metric='alpha', summary='none')
            #prefixes[prefix].append("mean_dist_to_bound_in")
            wandb.define_metric(f"{prefix}median_dist_to_bound_in", step_metric='alpha', summary='none')
            #prefixes[prefix].append("median_dist_to_bound_in")
            wandb.define_metric(f"{prefix}mean_dist_to_bound_out", step_metric='alpha', summary='none')
            #prefixes[prefix].append("mean_dist_to_bound_out")
            wandb.define_metric(f"{prefix}median_dist_to_bound_out", step_metric='alpha', summary='none')
            #prefixes[prefix].append("median_dist_to_bound_out")
            wandb.define_metric(f"{prefix}mean_eps", step_metric='alpha', summary='none')
            prefixes[prefix].append("mean_eps")

        registered_overall = (prefixes[prefix_overall], prefix_overall)
        registered_per_ts = (prefixes[prefix_per_ts], prefix_per_ts)
        return registered_overall, registered_per_ts

    @staticmethod
    def log_rolling_metrics(rolling_dfs, eval_config):
        if eval_config['rolling_per_ts']:
            tables = {}
            for key, df_list in rolling_dfs.items():
                tables[f"{key}rolling_metrics"] = wandb.Table(dataframe=pd.concat(df_list))
            wandb.log(tables)
        if eval_config['rolling_overall']:
            for key, df_list in rolling_dfs.items():
                for df in df_list:
                    df['ts_key'] = key.split("_", 1)[1]
            wandb.log({f"RollingEval/rolling_metrics": wandb.Table(dataframe=pd.concat(
                itertools.chain.from_iterable(rolling_dfs.values())))})
        if eval_config['rolling_as_list']:
            list_of_tables = []
            for key, df_list in rolling_dfs.items():
                for df in df_list:
                    df['ts_key'] = key.split("_", 1)[1]
                list_of_tables.append(wandb.Table(dataframe=pd.concat(df_list)))
            wandb.log({"RollingEval/rolling_metrics_list": list_of_tables})

    @staticmethod
    def _evaluate_single_ts(uc_service: UncertaintyService, dataset: ChronoSplittedTsDataset, alpha,
                            other_datasets: List[TsDataset]):
        # Pre Prediction step for a certain dataset, alpha
        LOGGER.info("Execute Pre Prediction Step.")
        start_step, pre_predict_len, max_window_len, eps = uc_service.pre_predict(dataset, alpha, other_datasets)

        # Iterate stepwise through the evaluation data
        X = dataset.X_full
        Y = dataset.Y_full
        pred_steps = X.shape[0] - start_step - pre_predict_len
        LOGGER.info(f"Start Prediction ({pred_steps} steps).")
        prediction_log = []
        prediction_log_artifacts = []
        for pred_step in tqdm(range(pred_steps)):
            overall_step = pred_step+start_step+pre_predict_len
            start_past = max(0, overall_step-max_window_len)
            pred_data = PIPredictionStepData(
                ts_id=dataset.ts_id,
                X_step=X[overall_step].unsqueeze(0),
                X_past=X[start_past:overall_step],
                Y_past=Y[start_past:overall_step],
                eps_past=(torch.Tensor(eps[-max_window_len:]) if len(eps) > max_window_len else torch.Tensor(eps)) if eps is not None else None,
                step_offset_prediction=pred_step,
                step_offset_overall=overall_step,
                alpha=alpha,
                mix_ts=uc_service.get_mix_data_service(dataset.ts_id, alpha).pack_mix_inference_data(
                    other_datasets, start_past=start_past, overall_step=overall_step)
            )
            Y_step = Y[overall_step].unsqueeze(0)
            prediction = uc_service.predict_step(Y_step, pred_data)
            if eps is not None:
                eps = eps + ((Y_step - prediction.fc_Y_hat).tolist())
            step_log_dict, step_log_artifact = Evaluator._log_step(
                prediction, pred_data, Y_step, rescale_param=dataset.Y_normalize_props)
            prediction_log.append(step_log_dict)
            prediction_log_artifacts.append(step_log_artifact)
        LOGGER.info(f"Finished Prediction - Log Results")
        return prediction_log, prediction_log_artifacts, eps

    @staticmethod
    def _log_step(prediction: PIModelPrediction, pred_data: PIPredictionStepData, Y_step, rescale_param):
        mean, std = rescale_param

        def rescale(Y):
            return (Y * std) + mean

        step_log_dict = OrderedDict()
        # TODO Handle situation when multiple timesteps as in one prediction
        step_log_dict['step'] = pred_data.step_offset_prediction
        step_log_dict['step_overall'] = pred_data.step_offset_overall
        step_log_dict['y_real'] = rescale(Y_step).item()
        step_log_dict['pi_low'] = rescale(prediction.pred_interval[0]).item()
        step_log_dict['pi_high'] = rescale(prediction.pred_interval[1]).item()
        step_log_dict['y_real_norm'] = Y_step.item()
        step_log_dict['pi_low_norm'] = prediction.pred_interval[0].item()
        step_log_dict['pi_high_norm'] = prediction.pred_interval[1].item()
        if prediction.fc_Y_hat is not None:
            step_log_dict['fc_y_hat'] = rescale(prediction.fc_Y_hat).item()
        if prediction.fc_interval is not None:
            step_log_dict['fc_pi_low'] = rescale(prediction.fc_interval[0]).item()
            step_log_dict['fc_pi_high'] = rescale(prediction.fc_interval[1]).item()
        #if pred_data.step_offset_prediction % 50 == 0:
        #    LOGGER.info(f"Prediction at step {pred_data.step_offset_prediction} (overall: {pred_data.step_offset_overall}:"
        #                f" {prediction}")
        step_log_artifact = dict()
        if prediction.uc_attention is not None:
            step_log_artifact['uc_attention'] = prediction.uc_attention
        return step_log_dict, step_log_artifact

    @staticmethod
    def _log_eval_finished(uc_service: UncertaintyService, prediction_log: List, prediction_artifacts: List,
                           dataset: TsDataset, eps: List[float], alpha: float, calib_artifact: PICalibArtifacts,
                           registered_per_ts, eval_config, is_first_dataset):
        # Set Config Options
        log_table = eval_config['pred_table']
        log_rolling_metrics = eval_config['rolling_overall'] or eval_config['rolling_per_ts'] or eval_config['rolling_as_list']
        log_pred_vega = eval_config['pred_vega']
        log_pred_vega_add_calib = eval_config.get('pred_vega_add_calib', False)
        log_att_vega = eval_config['att_plot_vega']
        log_pred_matplotlib = eval_config['pred_matplot']
        log_att_matplotlib = eval_config['att_hist_matplot']
        log_eps_matplotlib = eval_config['eps_matplot']
        plot_ensemble_matplotlib = eval_config['ensemble_matplot']
        print_full_ts_matplotlib = eval_config['pred_full_ts_matplot']
        print_pyplot_figs = eval_config['pypot_from_matplot']

        registered_for_ts_log, ts_log_prefix = registered_per_ts
        ts_log_prefix_plots = ts_log_prefix[:-1] + "_plots/"
        y_means, y_stds = dataset.Y_normalize_props
        def rescale_Y(Y):
            if isinstance(Y, np.ndarray):
                Y = torch.tensor(Y)
            return (Y * y_stds) + y_means

        # Convert prediction artifacts
        if prediction_artifacts is not None:
            prediction_artifacts = {key: [dic[key] for dic in prediction_artifacts] for key in prediction_artifacts[0]}

        wandb_log = dict()
        # Log to Result Table
        result_df = pd.DataFrame(prediction_log)
        result_df['y_in_pi'] = result_df.apply(lambda row: row['pi_high'] >= row['y_real'] >= row['pi_low'], axis=1)
        result_df['y_to_pi_bound_dist'] = result_df.apply(
            lambda row: min(abs(row['pi_high'] - row['y_real']), abs(row['y_real'] - row['pi_low'])), axis=1
        )
        result_df['y_to_pi_bound_dist_norm'] = result_df.apply(
            lambda row: min(abs(row['pi_high_norm'] - row['y_real_norm']), abs(row['y_real_norm'] - row['pi_low_norm'])), axis=1
        )
        result_df['pi_width'] = (result_df['pi_high'] - result_df['pi_low']).abs()
        result_df['pi_width_norm'] = (result_df['pi_high_norm'] - result_df['pi_low_norm']).abs()
        if 'fc_y_hat' in result_df.columns:
            result_df['eps'] = result_df['y_real'] - result_df['fc_y_hat']
        if 'fc_pi_low' in result_df.columns:
            result_df['y_in_fc_pi'] = result_df.apply(
                lambda row: row['fc_pi_high'] >= row['y_real'] >= row['fc_pi_low'], axis=1
            )
            result_df['fc_pi_width'] = result_df['fc_pi_high'] - result_df['fc_pi_low']
        if log_table:
            wandb_log[f"{ts_log_prefix}Table"] = result_df

        winkler_score = (result_df['pi_width'].sum() + (2 * (result_df.loc[~ result_df['y_in_pi']]['y_to_pi_bound_dist'].sum()) / alpha)) / result_df.shape[0]
        winkler_score_norm = (result_df['pi_width_norm'].sum() + (2 * (result_df.loc[~ result_df['y_in_pi']]['y_to_pi_bound_dist_norm'].sum()) / alpha)) / result_df.shape[0]
        #
        # Calc Base Metrics
        #
        eval_metrics = {
            f'mean_coverage': result_df['y_in_pi'].astype(int).mean(),
            f'mean_coverage_eps': alpha - (1 - result_df['y_in_pi'].astype(int).mean()),
            f'mean_pi_width': result_df['pi_width'].mean(),
            f'mean_pi_sd': result_df['pi_width'].std(),
            f'median_pi_with': result_df['pi_width'].median(),
            f'mean_dist_to_bound_in': result_df.loc[result_df['y_in_pi']]['y_to_pi_bound_dist'].mean(),
            f'median_dist_to_bound_in': result_df.loc[result_df['y_in_pi']]['y_to_pi_bound_dist'].median(),
            f'mean_dist_to_bound_out': result_df.loc[~ result_df['y_in_pi']]['y_to_pi_bound_dist'].mean(),
            f'median_dist_to_bound_out': result_df.loc[~ result_df['y_in_pi']]['y_to_pi_bound_dist'].median(),
            f'winkler_score': winkler_score,
            f'winkler_score_norm': winkler_score_norm
        }
        if 'fc_pi_width' in result_df.columns:
            eval_metrics[f'mean_fc_pi_width'] = result_df['fc_pi_width'].mean()
            eval_metrics[f'median_fc_pi_width'] = result_df['fc_pi_width'].median()
        if 'eps' in result_df.columns:
            eval_metrics[f'mean_eps'] = result_df['eps'].mean()

        #
        # Calc Rolling Metrics
        #
        if log_rolling_metrics:
            if True:  # calc_rolling post-hoc in result analysis
                step_wise_vals = pd.DataFrame(result_df[['y_in_pi', 'pi_width']])
                step_wise_vals['alpha'] = alpha
                rolling_df = step_wise_vals
            else:
                roll_window_size = eval_config['rolling_window']
                rolling_df = Evaluator._calc_rolling(
                    df_parts=[result_df['y_in_pi'].astype(int), result_df['y_in_pi'].astype(int),
                              result_df['pi_width'], result_df['pi_width']],
                    calc_funcs=[
                        lambda x: x.mean(),
                        lambda x: alpha - (1 - x.mean()),
                        lambda x: x.mean(),
                        lambda x: x.std()
                    ],
                    cols=["roll_coverage", "roll_coverage_eps", "roll_pi_mean_width", "roll_pi_sd"],
                    window_size=roll_window_size,
                )
                rolling_df['alpha'] = alpha
        else:
            rolling_df = None

        # Save Metrics with according prefix
        eval_with_prefix = dict(alpha=alpha)
        for key, value in eval_metrics.items():
            if key in registered_for_ts_log:
                eval_with_prefix[f"{ts_log_prefix}{key}"] = value

        wandb_log.update(eval_with_prefix)
        LOGGER.info(f"Evaluation metrics (alpha {alpha}: {eval_metrics}")

        #
        # Plot Prediction/Attention Plot VEGA
        #
        if log_pred_vega:
            raise ValueError("Not supported!")

        if log_att_vega and 'uc_attention' in prediction_artifacts and is_first_dataset:  # Only For first dataset
            raise ValueError("Not supported!")

        #
        # Plot Prediction (with Calib) Matplotlib
        #
        def print_prediction(ax):
            ax.fill_between(result_df['step_overall'], result_df['pi_low'], result_df['pi_high'], alpha=0.25, color='blue')
            if 'fc_y_hat' in result_df.columns:
                ax.plot(result_df['step_overall'], result_df['fc_y_hat'], label=f'Y Hat', color='red', linestyle='dashed')
            if 'fc_pi_low' in result_df.columns:
                ax.fill_between(result_df['step_overall'], result_df['fc_pi_low'], result_df['fc_pi_high'], alpha=0.15,
                                color='red')
            if calib_artifact.fc_Y_hat is not None:
                ax.plot(np.arange(start=dataset.calib_step, stop=dataset.calib_step+dataset.no_calib_steps),
                        rescale_Y(calib_artifact.fc_Y_hat), label=f'Y Hat (Calib)', color='lime', linestyle='dashed')
            if calib_artifact.fc_interval is not None:
                ax.fill_between(np.arange(start=dataset.calib_step, stop=dataset.calib_step+dataset.no_calib_steps),
                                rescale_Y(calib_artifact.fc_interval[0].squeeze()), rescale_Y(calib_artifact.fc_interval[1].squeeze()),
                                alpha=0.15, color='lime')
            if 'Y_hat_first_window' in calib_artifact.add_info:
                Y, start, end = calib_artifact.add_info['Y_hat_first_window']
                ax.plot(np.arange(start=start, stop=end), Y, label=f'Y Hat (Window)', color='darkgreen', linestyle='dashed')

        if log_pred_matplotlib:
            fig, ax = plt.subplots(figsize=(30, 5))
            ax.plot(np.arange(start=dataset.first_prediction_step, stop=dataset.no_of_steps),
                    rescale_Y(dataset.Y_full[dataset.first_prediction_step:]), label=f'Real Y', color='black', linestyle='dashed')
            print_prediction(ax)
            ax.set_xlim(dataset.first_prediction_step, dataset.no_of_steps)
            wandb_log[f"{ts_log_prefix_plots}Prediction_Plot (Img)"] = wandb.Image(fig, caption=f"Alpha: {alpha}")
            ax.set_title(f"Prediction Plot (Alpha: {alpha})")
            if print_pyplot_figs:
                wandb_log[f"{ts_log_prefix_plots}Prediction_Plot"] = fig

        #
        # Plot Epsilon Matplotlib
        #
        if log_eps_matplotlib and eps is not None:
            if len(eps[0]) == 1:
                eps = [item for sublist in eps for item in sublist]
            else:
                raise ValueError("Multi Epsilson not supported yet")
            fig, ax = plt.subplots(figsize=(30, 5))
            if calib_artifact.eps is not None:
                ax.scatter(np.arange(start=dataset.calib_step, stop=dataset.calib_step+dataset.no_calib_steps),
                           (calib_artifact.eps * y_stds.item()), label=f'Eps (Calib)', color='lime')

            eps_start = dataset.test_step if True else dataset.calib_step   # TODO
            ax.fill_between(result_df['step_overall'],
                            result_df['pi_low'] - rescale_Y(dataset.Y_full[result_df['step_overall'][0]:, 0]).numpy(),
                            result_df['pi_high'] - rescale_Y(dataset.Y_full[result_df['step_overall'][0]:, 0]).numpy(),
                            alpha=0.25, color='blue')
            ax.scatter(np.arange(start=eps_start, stop=eps_start+len(eps)), (np.array(eps) * y_stds.item()))
            ax.set_xlim(dataset.first_prediction_step, dataset.no_of_steps)
            wandb_log[f"{ts_log_prefix_plots}Epsilon_Plot (Img)"] = wandb.Image(fig, caption=f"Alpha: {alpha}")
            ax.set_title(f"Epsilon Plot (Alpha: {alpha})")
            if print_pyplot_figs:
                wandb_log[f"{ts_log_prefix_plots}Epsilon:Plot"] = fig

        #
        # Plot Predictions + Train data - Matplotlib
        #
        if not isinstance(dataset, BoostrapEnsembleTsDataset) and print_full_ts_matplotlib:
            # Only relevant if train data is seperate
            fig, ax = plt.subplots(figsize=(60, 5))
            ax.plot(rescale_Y(dataset.Y_full), label=f'Real Y', color='black', linestyle='dashed')
            print_prediction(ax)
            ax.set_xlim(0, dataset.no_of_steps)
            wandb_log[f"{ts_log_prefix_plots}Full_TS_Plot (Img)"] = wandb.Image(fig)
            ax.set_title(f"Full TS Plot (Alpha: {alpha})")
            if print_pyplot_figs:
                wandb_log[f"{ts_log_prefix_plots}Full_TS_Plot"] = fig

        #
        # For Ensemble Legacy Model: Plot Ensemble Prediction Matplotlib
        #
        fc_service = uc_service._fc_service
        if plot_ensemble_matplotlib and fc_service.has_ensemble:
            raise ValueError("Not supported!")

        #
        # Plot Attention Histograms Matplotlib
        #
        if log_att_matplotlib and 'uc_attention' in prediction_artifacts:
            raise ValueError("Not supported!")

        wandb.log(wandb_log)
        return eval_metrics, rolling_df

    @staticmethod
    def _calc_rolling(df_parts, calc_funcs, cols, window_size):
        rolling_parts = {'pred_step': np.arange(window_size-1, df_parts[0].shape[0])}
        for idx, func in enumerate(calc_funcs):
            result = func(df_parts[idx].rolling(window_size))
            result = result[window_size-1:]  # Cut part before full window
            rolling_parts[cols[idx]] = result.to_numpy()
        return pd.DataFrame(rolling_parts)


if __name__ == "__main__":
    my_app()
