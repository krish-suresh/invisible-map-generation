import numpy as np
import subprocess
import os
import matplotlib.pyplot as plt
import pandas as pd


# parameter to sweep
with open("noise_data.csv", 'w') as f:
    f.write("n,unopt,opt\n")
    for noise in np.linspace(0, 50, 100):
# run the generate script on each param and extract the file name from the output
        data = os.popen(f'python3 /home/ksuresh/invisible-map-generation/run_scripts/generate_datasets.py --e_cp "3, 5" --e_zw 7 --e_xw 9 --xzp 1.5 -t "occam" --obs_noise {noise} --odom_noise "({noise},{noise},{noise},{noise})"').read()
        path = data[24:44].split("'")[0]
        print(path)
        proc = subprocess.Popen([f'python3 run_scripts/graph_manager_user.py -u -p "generated/{path}.json" --pso 0 -g -w 5'], stdout=subprocess.PIPE, shell=True)
        (out, err) = proc.communicate()
        out = out.decode("unicode_escape")
        unoptimized = out.splitlines()[0].split(" ")[-1]
        optimized = out.splitlines()[2].split(" ")[-1]
        print(f"{noise},{unoptimized},{optimized}")
        f.write(f"{noise},{unoptimized},{optimized}\n")
        proc.kill()
# run the graph user script with the file name and extract the output value
data = pd.read_csv('noise_data.csv')
plt.plot(data["n"],data["opt"])
plt.show()
# save data
