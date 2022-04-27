import numpy as np
import subprocess
import os
import matplotlib.pyplot as plt
import pandas as pd
from itertools import permutations

# visualize = False

rot_noise_baseline = 0.001
with open("test_data/noise_data.csv", 'w') as f:
    f.write("name,min_gt,obs,trans,rot\n") #np.exp(np.linspace(-5,5,10))
    for obs_noise_ratio in np.linspace(0.05, 1, 3): # ratio between odo_noise_ratio and obs_noise_ratio
        for odo_noise_ratio in np.linspace(10, 200, 10): # ratio between translational and rotational noise
            translational_odom_noise = rot_noise_baseline*odo_noise_ratio
            obs_noise = translational_odom_noise*obs_noise_ratio
            print(f"obs_noise: {obs_noise}, t_noise {translational_odom_noise}, rot_noise: {rot_noise_baseline}")
            data = os.popen(f'python3 /home/ksuresh/invisible-map-generation/run_scripts/generate_datasets.py -t "3line" --obs_noise {obs_noise} --odom_noise "({translational_odom_noise},{translational_odom_noise},{translational_odom_noise},{rot_noise_baseline})"{" -v" if visualize else ""}').read()
            
            path = data[24:44].split("'")[0]
            print(path)

            proc = subprocess.Popen([f'python3 run_scripts/graph_manager_user.py -u -p "generated/{path}.json" --pso 0 -g -s {" -v" if visualize else ""}'], stdout=subprocess.PIPE, shell=True)
            (out, err) = proc.communicate()
            out = out.decode("unicode_escape")
            min_gt = out.strip()
            # delta = out.splitlines()[6].split(" ")[8]
            # print(f"{ground_truth_metric},{delta}")
            # # f.write(f"{noise},{unoptimized},{optimized}\n")
            f.write(f"{path},{min_gt},{obs_noise},{translational_odom_noise},{rot_noise_baseline}\n")

            proc.kill()
# data = pd.read_csv('test_data/noise_data.csv')
# fig = plt.figure()
# ax = fig.add_subplot(projection='3d')

# ax.scatter(data["trans"]/data["rot"], data["obs"]/data["trans"], data["min_gt"])

# plt.show()

data = pd.read_csv('test_data/generated_905216032.csv')

a = list(permutations([0.01, 3,0.01, 3,0.01, 3], 3))
res = []
[res.append(x) for x in a if x not in res]
print(res)
for p in res:
    f = data[(data['lin']==p[0]) & (data['accel']==p[1]) & (data['grav']==p[2])]
    plt.plot(f["odo"], f["gtd"])
plt.show()