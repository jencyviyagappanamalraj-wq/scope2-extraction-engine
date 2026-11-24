# Generalizing HVAC Control With Domain Randomized Reinforcement Learning

Pablo Boitel

pablo.boitel.1@ens.etsmtl.ca École de technologie supérieure (ÉTS) Montréal, Québec, Canada

Kun Zhang

kun.zhang@etsmtl.ca École de technologie supérieure (ÉTS) Montréal, Québec, Canada

## Abstract

Deploying advanced HVAC (Heating, Ventilation and Air Conditioning) controllers at scale remains difficult because performance often depends on accurate building models or per-site retuning. We propose NOMAD-RL (Neural Online Meta-Adaptation for Dynamics), a general-purpose Reinforcement Learning (RL) controller designed to transfer across heterogeneous thermal zones through a universal, non-invasive thermostat interface. The controller acts on temperature setpoints from zone measurements and forecasts, while a recurrent policy supports online adaptation under partial observability.

Our main contribution is an adaptive domain randomization scheme based on physics-informed normalizing flows, which models correlated and multimodal distributions of thermal-zone parameters while maintaining physical plausibility and controllability. This produces a realistic and progressively adaptive training curriculum that improves transfer across buildings. We evaluate NOMAD-RL against a constant-setpoint PID controller, RL without domain randomization, and MPC in singleand multi-zone settings. NOMAD-RL consistently outperforms the PID and non-randomized RL baselines, and approaches the performance of a well-tuned MPC, especially in the more challenging multi-zone case. These results highlight the potential of adaptive, physics-informed domain randomization for robust and transferable HVAC control.

## CCS Concepts

• Applied computing → Engineering; • General and reference → General conference proceedings; • Computing methodologies → Control methods.

## Keywords

Keywords: HVAC, Reinforcement learning, Control, Domain randomization

ACM Reference Format: Pablo Boitel and Kun Zhang. 2026. Generalizing HVAC Control With Domain Randomized Reinforcement Learning. In ACM Sustainability Week 2026 (ACM Sustainability Week Companion ’26), June 22–25, 2026, Banff, AB, Canada. ACM, New York, NY, USA, 4 pages. https://doi.org/10.1145/3765611. 3815359

This work is licensed under a Creative Commons Attribution 4.0 International License. ACM Sustainability Week Companion ’26, Banff, AB, Canada © 2026 Copyright held by the owner/author(s).

ACM ISBN: 979-8-4007-2199-1/2026/06

https://doi.org/10.1145/3765611.3815359

## 1 Introduction

Buildings account for 30% of global final energy use and 26% of energy-related $CO_2$ emissions [6], making HVAC control a promising lever for reducing both operating costs and emissions.

Model Predictive Control (MPC) remains the reference approach because it can explicitly balance comfort and energy use under operational constraints. However, its large-scale deployment is limited by the effort required to derive, calibrate, and maintain accurate models, especially beyond simple single-zone settings [3, 4]. Reinforcement Learning (RL) has therefore emerged as an alternative. Yet RL offers little advantage when it also relies on highly calibrated simulators: MPC depends on models explicitly, whereas RL depends on them implicitly through training environments.

This motivates a different objective for RL in HVAC control: learn controllers that transfer across heterogeneous thermal zones. The key intuition is that many physically different zones, exhibit similar closed-loop behavior when controlled through a thermostat interface. A controller trained across varied zones, while retaining memory of past dynamics and adapting online, can therefore learn transferable strategies rather than zone-specific heuristics.

We propose NOMAD-RL (Neural Online Meta-Adaptation for Dynamics), a general-purpose RL controller designed for this setting. NOMAD-RL acts through a universal thermostat interface and uses recurrent policies to handle partial observability and online adaptation. To promote transfer, training relies on adaptive domain randomization over thermal-zone dynamics. Rather than sampling simulator parameters independently, we model their joint distribution with neural spline flows and constrain it with physicsinformed regularization so that training remains both diverse and physically plausible.

Our contributions are fourfold. First, we introduce a flow-based adaptive domain randomization scheme that captures correlated and multimodal distributions of thermal-zone parameters. Second, we enforce physical plausibility and controllability through physicsinformed regularization. Third, we propose a stable training objective based on a context-normalized reward and a Boltzmann target with gradient-refined candidates, improving sample efficiency in high dimension. Fourth, we combine this curriculum with a recurrent RL controller that exploits interaction history and forecasts to support rapid adaptation under partial observability.

Experiments in both single-zone and multi-zone settings show that NOMAD-RL consistently outperforms constant-setpoint and non-randomized RL baselines, while approaching the performance of a well-tuned MPC. These results suggest that adaptive, physicsinformed domain randomization can make RL a practical path toward robust and transferable HVAC control across diverse buildings.

https: //doi.org/10.1145/3765611.3815359

ACM Sustainability Week Companion ’26, June 22–25, 2026, Banff, AB, Canada

Pablo Boitel and Kun Zhang

![Figure 1](images/page2_figure_1.png)

Figure 1: Architecture of NOMAD-RL

## 2 Method

We consider a general and practical control setting in which the agent interacts with the building through a standard thermostat interface. Rather than directly controlling the heating plant, the controller observes zone temperature and power, adjusts the temperature setpoint. This supervisory control approach improves safety, since actions can be constrained within a reasonable comfort range, preserves interpretability, and avoids learning low-level actuation already handled by existing PID loops. It also promotes transfer across buildings, as closed-loop thermostat-level behavior tends to be more consistent than plant dynamics. See Fig.1 for an overview of the architecture of NOMAD-RL and details are given below.

### 2.1 Thermal zone modeling

In this work, we use a generic thermal-zone simulator based on a fifth-order resistance-capacitance (RC5) model coupled with a hydronic heat pump and a low-level PID controller; following [1]. The model is implemented in JAX to support efficient simulation and automatic differentiation.

The parameters include those of building envelope, internal mass, heat pump characteristics, and PID gains, resulting in a 25dimensional domain space. This compact but expressive setup provides a convenient testbed for evaluating transfer, robustness, and adaptive domain randomization, while still allowing strong MPC baselines to be computed on the nominal model.

### 2.2 Reinforcement learning controller

We formulate HVAC control as a partially observable sequential decision-making problem (POMDP). Since the controller does not observe the full thermal states of the building, it must act from limited measurements and delayed effects. This motivates a recurrent RL policy to infer hidden dynamics from interaction history. At each step, the agent observes the current zone temperature, power consumption, comfort bounds, outdoor conditions, occupancy, electricity price, and time features, together with short-term forecasts of the main exogenous inputs. Rather than directly controlling the heating plant, the policy outputs an incremental update to the thermostat setpoint, clipped to a safe comfort range. This preserves interpretability, simplifies deployment, and keeps low-level actuation handled by the existing PID loop.

Forecast trajectories are encoded with a temporal convolution module and fused with current measurements into a compact latent representation. This representation is then passed to a recurrent LSTM-based actor–critic policy, providing the required memory to adapt online to partially observed and changing dynamics. We train the controller with Recurrent PPO, using the standard actor–critic implementation from the SB3-Contrib library.

The reward reflects the operational HVAC objective: minimizing electricity cost, maintaining comfort and avoiding excessive actuator saturation. To make training comparable across heterogeneous buildings and operating conditions, we use a normalized reward relative to a fixed-setpoint PID reference rollout computed at the start of each episode. This replay-based normalization differs from the standard online RL setting, where trajectories are stochastic, nonreproducible, and cannot be exactly replayed. It therefore provides a stable learning signal under domain randomization and measures improvement over a simple but realistic reference controller.

### 2.3 Domain randomization for RL training

To promote transfer across heterogeneous thermal zones, we train the policy under domain randomization. At the beginning of each episode, all the simulator parameters $\xi \in \mathbb{R}^{25}$ are sampled from a distribution $p_\phi(\xi)$, while the policy does not observe $\xi$ directly. Training therefore optimizes expected return over a family of environments rather than a single nominal model.

The main challenge is to adapt $p_\phi$ so that it generates informative and realistic environments: if the distribution is too narrow, the

policy overfits; if it is too broad, training becomes unstable. Building upon previous work [2, 5, 7], we address this challenge with an adaptive domain randomization scheme based on normalizing flows.

We represent $p_\phi(\xi)$ with a neural spline flow, which can model correlated, multimodal, and high-dimensional parameter distributions. Rather than optimizing the flow directly through noisy gradients, we construct an empirical Boltzmann target over candidate environments and fit the flow to it by weighted maximum likelihood, yielding a more stable update.

To remain effective in high-dimensional settings, candidate parameters are first sampled uniformly over the admissible domain, then refined through a few gradient steps. Each random draw thus becomes a short local search trajectory, which improves coverage of the parameter space. Each candidate is evaluated using three components: the return under the current policy $J_\theta(\xi)$, a physical plausibility cost $C_P(\xi)$, and a surprise bonus $\log p_\phi(\xi)$ that promotes exploration. The resulting utility $U(\xi)$, which measures the relevance of a parameter vector (higher is better), is defined as

$$ U(\xi) = J_{\theta}(\xi) - \lambda_{\text{phys}} C_P(\xi) + \lambda_{\text{sur}}(-\log p_{\phi}(\xi)) $$

This utility yields the Boltzmann weights, i.e., the analytical solution of the associated expectation-maximization problem:

$$ w_i = \text{softmax}\left(\frac{U(\xi_i)}{\tau}\right)_i $$

where 𝜏 controls how concentrated the distribution is.

The physical cost $C_P(\xi)$ constrains the curriculum to realistic and controllable regions of the parameter space by penalizing implausible heat-pump efficiencies, poor comfort regulation under a fixed-setpoint baseline controller, and excessive actuator saturation. Figure 2 shows a 2D slice of the resulting learned distribution in the $(gA, C_i)$ space, i.e., equivalent solar area and internal capacitance.

![Figure 2](images/page3_figure_2.png)

Figure 2: 2D slice of the learned parameter-flow distribution in the (𝑔𝐴,𝐶 𝑖 ) space over the target Boltzmann distribution (left) and their correlation (right)

The parameter distribution is then updated from the weighted candidate set using a Boltzmann-weighted negative log-likelihood with a KL trust region:

$$ \min_{\phi} \quad L(\phi) = -\sum_{i=1}^{N} w_i \log p_{\phi}(\xi_i) + \beta D_{\mathrm{KL}}(p_{\phi} \| p_{\phi_{\mathrm{old}}}) $$

This stabilizes successive updates while allowing the distribution to expand gradually as the policy improves. Overall, NOMAD-RL alternates between updating the recurrent policy on environments sampled from the current flow and updating the flow from refined

ACM Sustainability Week Companion ’26, June 22–25, 2026, Banff, AB, Canada

candidates scored by utility, yielding an adaptive curriculum over realistic thermal-zone dynamics (see the Algorithm 1 for the steps).

### 2.4 Training configuration

Training was limited to a preliminary 6𝑒6-steps run on a single CPU/GPU, using Algorithm 1, with limited hyperparameter tuning.

Fig. 3 illustrates a representative NOMAD-RL training episode in our tuned GYM environment, showing the full sequence of actions and observations. High-price events (red) are periods the controller must anticipate, where power should be minimized. Lowoccupancy periods (blue) indicate times when temperature may temporarily drift outside comfort bounds without penalty. The episode starts with a warm-up phase (yellow). The figure also reports the constant-setpoint baseline, weather forecasts, power and COP trajectories, and an example occupancy profile.

**Algorithm 1** NOMAD-RL  
**Require:** policy parameters $\theta$, flow parameters $\phi$  
1: **for** $n = 1$ to $N$ **do**  
2: Train Policy($\theta$, $p_\phi$)  
3: $\mathcal{D} \leftarrow$ Sample And Refine($\mathcal{X}$, $U$)  
4: $w \leftarrow$ Boltzmann Weights($\mathcal{D}$, $U$)  
5: Update Flow($\phi$, $\mathcal{D}$, $w$)  
6: **end for**

## 3 Results and evaluation of NOMAD-RL

After training, we evaluate **NOMAD-RL** against a constant setpoint PID controller, a recurrent PPO baseline trained without domain randomization, and MPC. **Costs are reported in euros, reflecting energy consumption, comfort, and saturation**. Percentages use $(C_\text{ref} - C_\text{NOMAD})/C_\text{ref} \times 100$; negative values mean that **NOMAD-RL** is more costly than the reference.

### 3.1 Single-zone setting

We first compare NOMAD-RL with a recurrent PPO agent trained on a single fixed environment. Both policies are evaluated on the nominal zone and under randomized dynamics to assess robustness and out-of-domain generalization. We also compare them with MPC and a constant set-point PID controller on a representative sampled zone. In this setting, MPC has access to the exact thermal-zone model and serves as a near-global optimum reference.

As shown in Fig. 4, both RL controllers clearly outperform the constant set-point PID baseline. NOMAD-RL reduces cost by 70.4% relative to PID, but is 8.7% more costly than recurrent PPO on the nominal zone.

On the sampled zone with shifted dynamics (Fig. 5), NOMAD- RL reduces cost by 60.1% relative to PID and 51.4% relative to recurrent PPO, while being 46.6% more costly than MPC. This is notable because MPC has access to the exact model and therefore approximates the global optimum in this setting.

### 3.2 Multi-zone setting

WecompareNOMAD-RLwithper-zoneMPCandconstant-setpoint PID on a 3-zone building with inter-zone thermal coupling, using the building-level cumulative cost aggregated over the three zones.

ACM Sustainability Week Companion ’26, June 22–25, 2026, Banff, AB, Canada

Pablo Boitel and Kun Zhang

![Figure 3](images/page4_figure_3.png)

Figure 3: Behavior of the NOMAD-RL controller during training.

![Figure 4](images/page4_figure_4.png)

Figure 4: NOMAD-RL vs. recurrent PPO without domain ran- domization on the nominal thermal zone.

![Figure 5](images/page4_figure_5.png)

Figure 5: NOMAD-RL vs. recurrent PPO without domain ran- domization vs. MPC on a sampled thermal zone.

NOMAD-RL remains effective in this more challenging setting: it reduces cost by 67.9% relative to constant-setpoint PID and is 14.5% more costly than MPC. The gap to MPC narrows as the problem becomes more complex, suggesting robustness in complex environments.

## 4 Conclusion

This work addresses a central challenge in scalable HVAC control: achieving robust performance across heterogeneous thermal zones without per-site modeling or retuning. We introduced NOMAD- RL, a general-purpose RL controller built on a universal thermostat interface that preserves safety, interpretability, and practical deployability. This supervisory approach is non-invasive and lightweight enough for embedded devices or connected thermostats.

Our main contribution is an adaptive domain randomization framework based on physics-informed normalizing flows. By learning multimodal, correlated distributions over thermal-zone parameters while enforcing physical plausibility and controllability, the framework generates a realistic and progressively expanding training curriculum. Combined with a recurrent policy leveraging past observations and forecasts, it yields transferable control policies capable of rapid online adaptation in a meta-learning setting.

Results support this approach. In single-zone settings, NOMAD- RL outperforms PID and non-randomized RL baselines, while remaining within 46.6% of MPC on the sampled zone. In multi-zone settings, it reduces PID cost by 67.9% and remains within 14.5% of MPC. This suggests that NOMAD-RL provides a path toward generalizable HVAC control without accurate models.

Future work will focus on improving performance and real-world readiness through further training, hyperparameter tuning, expansion of the simulator and dataset pipeline, and validation on real buildings. We also plan to evaluate NOMAD-RL on HVAC benchmarks for generalization, and explore larger architectures, transformers, and flow matching for domain randomization. The implementation used in this work is publicly available.1

## References

[1] Arroyo et al. 2022. Reinforced model predictive control (RL-MPC) for building energy management. Applied Energy 309 (March 2022), 118346. doi:10.1016/j. apenergy.2021.118346 [2] Curtis et al. [n.d.]. Flow-based Domain Randomization for Learning and Sequencing Robotic Skills. ([n.d.]). [3] Drgoňa et al. 2020. All you need to know about model predictive control for buildings. Annual Reviews in Control 50 (Jan. 2020), 190–232. doi:10.1016/j.arcontrol. 2020.09.001 [4] Nagy et al. 2023. Ten questions concerning reinforcement learning for building energy management. Building and Environment 241 (Aug. 2023), 110435. doi:10. 1016/j.buildenv.2023.110435 [5] Tiboni et al. 2024. Domain Randomization via Entropy Maximization. doi:10. 48550/arXiv.2311.01885 arXiv:2311.01885 [cs]. [6] International Energy Agency. [n.d.]. Buildings - Energy System. IEA website. https://www.iea.org/energy-system/buildings Accessed: 2026-01-29. [7] OpenAI. 2019. Solving Rubik’s Cube with a Robot Hand. doi:10.48550/arXiv.1910. 07113 arXiv:1910.07113 [cs].

1https://github.com/paabL/NOMAD-RL
