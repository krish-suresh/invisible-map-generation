"""
Contains the GraphGenerator class used for generating artificial datasets for optimization. See the generate_datasets.py
script for a CLI interface using this class.
"""

import json
import random
from enum import Enum, auto
from typing import Callable, Tuple, Optional, List, Dict, Union

import matplotlib
import matplotlib.pyplot as plt
import numpy as np
from g2o import SE3Quat
from mpl_toolkits.mplot3d import Axes3D
import cv2.cv2 as cv2

from map_processing.cache_manager import CacheManagerSingleton, MapInfo
from map_processing.data_models import UGDataSet, UGTagDatum, UGPoseDatum, GTDataSet, GTTagPose
from map_processing.transform_utils import norm_array_cols, FLIP_Y_AND_Z_AXES, AR_TO_OPENCV, transform_matrix_to_vector, \
    transform_vector_to_matrix

matplotlib.rcParams['figure.dpi'] = 500

SQRT_2_OVER_2 = np.sqrt(2) / 2


class GraphGenerator:
    """Generates synthetic datasets simulating tag observations.

    Attributes:
        _path: Defines a parameterized path. Takes as input an N-length vector of parameters to evaluate the curve at,
         and returns a 3xN array where the rows from top to bottom are the x, y, and z coordinates respectively.
        _path_type: The type of the `_path` attribute as specified by the `GraphGenerator.PathType` enumeration.
        _t_max: Max parameter value to use when evaluating a parameterized path. If a recorded path is prescribed, then
         this is ignored.
        _delta_t: For a parameterized path, this gives the time delta used between each of the points. If the path is
         a recorded path, then this value is set to 0 arbitrarily.
        _odometry_t_vec: The vector of pose time stamps, either generated from _t_max and _n_poses in the case of a
         parameterized path or copied from the 'timestamp' fields of the data set otherwise.
        _n_poses: Number of poses to sample a parameterized path at (referenced as N in the array dimensions described
         for other attributes); if the path is a recorded path, then this is set to the number of poses in that data
         set.
        _tag_poses: A Mx4x4 array of M tag poses defined in the global reference frame using homogenous transform
         matrices.
        _dist_threshold: Maximum distance from which a tag can be considered observable.
        _aoa_threshold: Maximum angle of attack (in radians) from which a tag can be considered observable. The angle
         of attack is calculated as the angle between the z-axis of the tag pose and the vector from the tag to the
         phone.
        _odometry_poses: Nx4x4 array of N homogenous transforms representing the poses sampled along the path.
        _observation_poses: A list of length N where N is the number of poses sampled along the path. Each element of
         the list is a dictionary mapping tag IDs to their corresponding transforms that give their position in the
         reference frame of that pose. A key-value pair is only present in the dictionary if it is visible from that
         pose.
        _observation_pixels: Nx2x4 array containing the pixel coordinates corresponding to the pose observations
         recorded in _observation_poses.
        _tag_corners_in_tag: Applying the tag-in-phone transform with the y and z-axes negated to this 4x4 matrix yields
         a 4x4 matrix where the first 3 rows correspond to the tag corners' locations in the (y- and z-axis negated)
         reference frame.
    """

    class PathType(Enum):
        RECORDED = auto()
        PARAMETERIZED = auto()

    class OdomNoiseDims(Enum):
        X = 0
        Y = 1
        Z = 2
        RVert = 3

        @staticmethod
        def ordering() -> List:
            ordered = [GraphGenerator.OdomNoiseDims.X, GraphGenerator.OdomNoiseDims.Y, GraphGenerator.OdomNoiseDims.Z,
                       GraphGenerator.OdomNoiseDims.RVert]
            return ordered

    # noinspection GrazieInspection
    TAG_DATASETS: Dict[str, Dict[int, np.ndarray]] = {
        "3line": {
            0: np.array([
                [1, 0, 0, -3],
                [0, 1, 0, 0],
                [0, 0, 1, -4],
                [0, 0, 0, 1]
            ]),
            1: np.array([
                [1, 0, 0, 0],
                [0, 1, 0, 0],
                [0, 0, 1, -4],
                [0, 0, 0, 1]
            ]),
            2: np.array([
                [1, 0, 0, 3],
                [0, 1, 0, 0],
                [0, 0, 1, -4],
                [0, 0, 0, 1]
            ])
        },
        "occam": {
            # The ground truth tags for the 6-17-21 OCCAM Room. Keyed by tag ID. Measurements in meters (measurements
            # were taken in inches and converted to meters by multiplying by 0.0254). Measurements are in a right-handed
            # coordinate system with its origin at the floor beneath tag id=0 (+Z pointing out of the wall and +X
            # pointing to the right).
            0: transform_vector_to_matrix(SE3Quat([0, 63.25 * 0.0254, 0, 0, 0, 0, 1]).to_vector()),
            1: transform_vector_to_matrix(
                SE3Quat([269 * 0.0254, 48.5 * 0.0254, -31.25 * 0.0254, 0, 0, 0, 1]).to_vector()),
            2: transform_vector_to_matrix(
                SE3Quat([350 * 0.0254, 58.25 * 0.0254, 86.25 * 0.0254, 0, SQRT_2_OVER_2, 0,
                         -SQRT_2_OVER_2]).to_vector()),
            3: transform_vector_to_matrix(
                SE3Quat([345.5 * 0.0254, 58 * 0.0254, 357.75 * 0.0254, 0, 1, 0, 0]).to_vector()),
            4: transform_vector_to_matrix(
                SE3Quat([240 * 0.0254, 86 * 0.0254, 393 * 0.0254, 0, 1, 0, 0]).to_vector()),
            5: transform_vector_to_matrix(
                SE3Quat([104 * 0.0254, 31.75 * 0.0254, 393 * 0.0254, 0, 1, 0, 0]).to_vector()),
            6: transform_vector_to_matrix(
                SE3Quat([-76.75 * 0.0254, 56.5 * 0.0254, 316.75 * 0.0254, 0, SQRT_2_OVER_2, 0,
                         SQRT_2_OVER_2]).to_vector()),
            7: transform_vector_to_matrix(SE3Quat([-76.75 * 0.0254, 54 * 0.0254, 75 * 0.0254, 0, SQRT_2_OVER_2, 0,
                                                   SQRT_2_OVER_2]).to_vector()),
        }
    }

    CAMERA_INTRINSICS_VEC = [
        1458.0604248046875,  # fx (camera focal length in the x-axis)
        1458.0604248046875,  # fy (camera focal length in the y-axis)
        924.9375,  # cx (principal point offset in the x-axis)
        725.906494140625  # cy (principal point offset along the y-axis)
    ]

    CAMERA_INTRINSICS = np.array([
        [CAMERA_INTRINSICS_VEC[0], 0, CAMERA_INTRINSICS_VEC[2]],
        [0, CAMERA_INTRINSICS_VEC[1], CAMERA_INTRINSICS_VEC[3]],
        [0, 0, 1]
    ])
    _double_camera_intrinsics = 2 * CAMERA_INTRINSICS

    TAG_CORNERS_SIZE_2 = np.transpose(np.array([
        [-1, 1, 0, 1],  # Top left
        [1, 1, 0, 1],  # Top right
        [1, -1, 0, 1],  # Bottom right
        [-1, -1, 0, 1],  # Bottom left
    ]).astype(float))

    TAG_CORNERS_SIZE_2_FOR_CV_PNP = np.array([
        [-1, 1, 0],
        [1, 1, 0],
        [1, -1, 0],
        [-1, -1, 0],
    ]).astype(float)

    PHONE_IN_FRENET = np.array([
        [0, -1, 0, 0],
        [0, 0, 1, 0],
        [-1, 0, 0, 0],
        [0, 0, 0, 1]
    ])

    PATH_LINEAR_DELTA_T = 0.0001  # Time span over which the path can be assumed to be approximately linear
    X_HAT_1X3 = np.array(((1, 0, 0),))
    Y_HAT_1X3 = np.array(((0, 1, 0),))
    Z_HAT_1X3 = np.array(((0, 0, 1),))
    BASES_COLOR_CODES = ("r", "g", "b")

    def __init__(self,
                 path_from: Union[Callable[[np.ndarray, Dict[str, float]], np.ndarray], UGDataSet], dataset_name: str,
                 parameterized_path_args: Optional[Dict[str, Union[float, Tuple[float, float]]]] = None,
                 t_max: float = 0, n_poses: int = 100, tag_poses: Optional[Dict[int, np.ndarray]] = None,
                 dist_threshold: float = 3.7, aoa_threshold: float = np.pi / 4, tag_size: float = 0.7,
                 odometry_noise: Optional[Dict[OdomNoiseDims, float]] = None, obs_noise_var: float = 0.0):
        """Initializes a GraphGenerator instance and automatically invokes the `generate` method.

        Args:
            path_from: If a callable, then it defines a parameterized path where the first positional argument of the
             callable is to be an N-length vector of parameters to evaluate the curve at, and returns a 3xN array where
             the rows from top to bottom are the x, y, and z coordinates respectively; the second positional argument is
             a dictionary specifying path parameters (the contents of which is function-specific). If a `UGDataSet`
             instance, then it defines a path and set of tags according to the data in the data set.
            dataset_name: String used as the name for the dataset when caching the ground truth data
            parameterized_path_args: Dictionary to pass as the second positional argument to the `path` if it is a
             callable (if the `path` argument is not a callable, then this argument is ignored).
            t_max: For a parameterized path, this is the max parameter value to use when evaluating the path.
            n_poses: Number of poses to sample a parameterized path at; if a recorded path is provided, then this
             argument is ignored.
            tag_poses: A dictionary mapping tag IDs to their poses in the global reference frame using homogenous
             transform matrices; if a recorded path is provided, then this argument overrides the data set's tag poses
             if it is not none.
            dist_threshold: Maximum distance from which a tag can be considered observable.
            aoa_threshold: Maximum angle of attack (in radians) from which a tag can be considered observable. The angle
             of attack is calculated as the angle between the z-axis of the tag pose and the vector from the tag to the
             phone.
            tag_size: Height/width dimension of the (square) tags in meters.
            obs_noise_var: Variance parameter for the observation model. Specifies the variance for the distribution
             from which pixel noise is sampled and added to the simulated tag corner pixel observations. Note that the
             simulated tag observation poses are re-derived from these noise pixel observations.

        Raises:
            ValueError - If `path_from` is a `UGDataSet` instance and `t_max` is negative.
            ValueError - If `path_from` is a `UGDataSet` instance and `n_poses` is <2
        """
        self._path: Union[Callable[[np.ndarray, Dict[str, float]], np.ndarray], UGDataSet] = path_from
        self._path_type = GraphGenerator.PathType.PARAMETERIZED if isinstance(self._path, Callable) else \
            GraphGenerator.PathType.RECORDED
        self._dataset_name: str = dataset_name
        self._dist_threshold = dist_threshold
        self._aoa_threshold = aoa_threshold

        self._parameterized_path_args: Optional[Dict[str, Union[float, Tuple[float, float]]]] = \
            dict(parameterized_path_args) if parameterized_path_args is not None else None

        if t_max < 0:
            raise ValueError("'t_max' argument cannot be less than 0")
        self._t_max = t_max

        if n_poses < 2:
            raise ValueError("'n_poses' argument cannot be less than 2")
        self._n_poses = n_poses if isinstance(self._path, Callable) else len(self._path.pose_data)

        self._tag_poses: Dict[int, np.ndarray]
        if tag_poses is not None:
            self._tag_poses = tag_poses
        else:
            if self._path_type is GraphGenerator.PathType.PARAMETERIZED:
                self._tag_poses = {}
            else:
                self._tag_poses = self._path.approx_tag_in_global_by_id

        self._tag_poses_arr = np.zeros((len(self._tag_poses), 4, 4))
        for i, pose in enumerate(self._tag_poses.values()):
            self._tag_poses_arr[i, :, :] = pose

        self._odometry_poses: Optional[np.ndarray] = None
        if self._path_type is GraphGenerator.PathType.PARAMETERIZED:
            self._odometry_t_vec = np.arange(0, self._t_max, self._t_max / self._n_poses)
            self._delta_t = self._t_max / (self._n_poses - 1)
        else:
            self._odometry_t_vec = self._path.timestamps
            self._delta_t = 0

        self._observation_poses: Optional[List[Optional[Dict[int, np.ndarray]]]] = None
        self._obs_from_poses: Optional[np.ndarray] = None
        self._observation_pixels: Optional[List[Optional[Dict[int, np.ndarray]]]] = None

        self._tag_corners_in_tag = GraphGenerator.TAG_CORNERS_SIZE_2
        self._tag_corners_in_tag[:3, :] *= tag_size / 2
        self._tag_corners_for_pnp = GraphGenerator.TAG_CORNERS_SIZE_2_FOR_CV_PNP * tag_size / 2

        self._cms: Optional[CacheManagerSingleton] = None

        self._odometry_noise_var: Dict[GraphGenerator.OdomNoiseDims, float] = \
            odometry_noise if odometry_noise is not None else {
                GraphGenerator.OdomNoiseDims.X: 0,
                GraphGenerator.OdomNoiseDims.Y: 0,
                GraphGenerator.OdomNoiseDims.Z: 0,
                GraphGenerator.OdomNoiseDims.RVert: 0,
            }
        self._obs_noise_var = obs_noise_var

        self.generate()

    # -- Public methods --

    def export(self) -> Tuple[UGDataSet, GTDataSet]:
        """
        Returns:
            A UGDataSet object that, when serialized, will contain the unprocessed graph json encoding of this
             artificial dataset generation.
        """
        # Construct data for the UGDataSet initialization
        pose_data: List[UGPoseDatum] = []
        tag_data: List[List[UGTagDatum]] = []
        tag_data_idx = -1
        for pose_idx in range(self._odometry_poses.shape[0]):
            pose_data.append(
                UGPoseDatum(
                    pose=list(self._odometry_poses[pose_idx, :, :].flatten(order="F")),
                    timestamp=self._odometry_t_vec[pose_idx],
                    # Intentionally skipping planes
                    id=pose_idx
                )
            )

            looped = False
            for tag_id in self._observation_poses[pose_idx].keys():
                if not looped:
                    tag_data.append(list())
                    tag_data_idx += 1
                looped = True

                tag_data[tag_data_idx].append(
                    UGTagDatum(
                        tag_corners_pixel_coordinates=list(
                            self._observation_pixels[pose_idx][tag_id].flatten(order="F")),
                        tag_id=tag_id,
                        pose_id=pose_idx,
                        camera_intrinsics=list(np.array(GraphGenerator.CAMERA_INTRINSICS_VEC)),
                        # Intentionally skipping position and orientation variance
                        timestamp=self._odometry_t_vec[pose_idx],
                        tag_pose=list(np.matmul(FLIP_Y_AND_Z_AXES,
                                                self._observation_poses[pose_idx][tag_id]).flatten(order="C")),
                        # Intentionally skipping joint covariance
                    )
                )

        # Construct data for the GTDataSet initialization: arbitrarily select a tag to use as the origin of the
        # coordinate system in which the rest of the tags are represented.
        ground_truth_tags = []
        origin_key: int = 0
        for key in self._tag_poses.keys():
            origin_key = key
            break
        if len(self._tag_poses) != 0:
            origin_inv = np.linalg.inv(self._tag_poses[origin_key])
        for item in self._tag_poses.items():
            # This point will only be reached if there are a nonzero number of tag poses (so origin_inv is guaranteed to
            # be defined
            # noinspection PyUnboundLocalVariable
            ground_truth_tags.append(
                GTTagPose(
                    tag_id=item[0],
                    pose=list(transform_matrix_to_vector(np.matmul(origin_inv, item[1])))
                )
            )

        return UGDataSet(
            # Intentionally skipping location data
            map_id="generated_" + str(random.randint(0, int(1e9))),  # arbitrary integer for unique id-ing
            # Intentionally skipping plane data
            pose_data=pose_data,
            tag_data=tag_data
        ), GTDataSet(
            poses=ground_truth_tags
        )

    def export_to_map_processing_cache(self) -> None:
        """Serializes the graph into a json file and saves it to the target destination as set by the TODO
        """
        map_obj, gt_obj = self.export()
        map_dict = map_obj.dict()
        map_str = json.dumps(map_dict, indent=2)

        map_name = map_dict["map_id"]
        print(f"Generated new data set '{map_name}' containing {map_obj.pose_data_len} poses, {map_obj.num_tags} "
              f"tags, and {map_obj.num_observations} observations")

        if self._cms is None:
            self._cms = CacheManagerSingleton()
        self._cms.cache_map(
            parent_folder="generated",
            map_info=MapInfo(
                map_name=map_name,
                map_json_name=map_name,
            ),
            json_string=map_str
        )
        self._cms.cache_ground_truth_data(gt_obj, dataset_name=self._dataset_name, corresponding_map_names=[map_name])

    def visualize(self, plus_minus_lim=5) -> None:
        """Visualizes the generated graph by plotting the path, the poses on the path, the tags, and the observations of
         those tags.

         TODO: add plot legend.

        Args:
            plus_minus_lim: Value for the x-, y-, and z-lim3d parameters of the matplotlib 3d axes.
        """
        path_samples = self._obs_from_poses[:, :3, 3].transpose()

        f: plt.Figure = plt.figure()
        ax: Axes3D = f.add_subplot(projection="3d")
        ax.set_xlabel("X")
        ax.set_ylabel("Y")
        ax.set_zlabel("Z")
        ax.axes.set_xlim3d(left=-plus_minus_lim, right=plus_minus_lim)
        ax.axes.set_ylim3d(bottom=-plus_minus_lim, top=plus_minus_lim)
        ax.axes.set_zlim3d(bottom=-plus_minus_lim, top=plus_minus_lim)

        plt.plot(path_samples[0, :], path_samples[1, :], path_samples[2, :])
        GraphGenerator.draw_frames((self._odometry_poses[:, :3, 3]).transpose(), self._odometry_poses[:, :3, :3], ax)
        GraphGenerator.draw_frames((self._tag_poses_arr[:, :3, 3]).transpose(), self._tag_poses_arr[:, :3, :3], ax,
                                   colors=("m", "m", "m"))

        # Get observation vectors in the global frame and plot them
        for i, dct in enumerate(self._observation_poses):
            pose = self._obs_from_poses[i, :, :]
            line_start = pose[:3, 3]
            for obs in dct.values():
                # If transforms are computed correctly, then obs_in_global should be equivalent to the original tag pose
                # definition
                obs_in_global = np.matmul(pose, obs)
                line_end = obs_in_global[:3, 3]
                ax.plot(
                    xs=[line_start[0], line_end[0]],
                    ys=[line_start[1], line_end[1]],
                    zs=[line_start[2], line_end[2]],
                    color="c"
                )
        plt.show()

    # noinspection Pydantic
    def generate(self) -> None:
        """Populate the _odometry_poses attribute with the poses sampled at the given parameter values, then the
        _observation_poses attribute is populated with the tag observations at each of the poses.

        Raises:
            ValueError - If `_path` is not a Callable or a UGDataSet.
        """
        if isinstance(self._path, Callable):
            positions = self._path(self._odometry_t_vec, self._parameterized_path_args)  # 3xN array
            # Nx3x3 array
            frenet_frames = GraphGenerator.frenet_frames(self._odometry_t_vec, self._path,
                                                         self._parameterized_path_args)
            true_poses = np.zeros((len(self._odometry_t_vec), 4, 4))  # Nx4x4
            true_poses[:, 3, 3] = 1
            true_poses[:, :3, 3] = positions.transpose()
            true_poses[:, :3, :3] = np.matmul(frenet_frames, GraphGenerator.PHONE_IN_FRENET[:3, :3])
            self._odometry_poses = self._apply_noise(true_poses)
            self._obs_from_poses = true_poses
        elif isinstance(self._path, UGDataSet):
            self._odometry_poses = self._apply_noise(self._path.pose_matrices)
            self._obs_from_poses = self._path.pose_matrices
        else:
            raise ValueError(f"'_path' attribute is not of a known type")

        self._observation_poses = []
        self._observation_pixels = []
        for i in range(self._n_poses):
            self._observation_poses.append(dict())
            self._observation_pixels.append(dict())
            for tag_id in self._tag_poses:
                tag_obs, tag_pixels = self._get_tag_observation(self._obs_from_poses[i, :, :], self._tag_poses[tag_id])
                if tag_obs.shape[0] == 0:  # True if no tags were visible
                    continue

                # If >=1 tags were visible, enter that information into the observation dictionaries
                self._observation_poses[i][tag_id] = tag_obs
                self._observation_pixels[i][tag_id] = tag_pixels

    # -- Private methods --

    def _apply_noise(self, true_poses: np.ndarray) -> np.ndarray:
        if true_poses.shape[0] <= 1:
            raise Exception("To apply noise, there must be >=2 poses")

        # The t^th sub-array of the pose_to_pose array contains the transform from the pose at time t-1 to t.
        pose_to_pose = np.zeros((true_poses.shape[0] - 1, 4, 4))
        for i in range(0, true_poses.shape[0] - 1):
            pose_to_pose[i, :, :] = np.matmul(np.linalg.inv(true_poses[i, :, :]), true_poses[i + 1, :, :])

        # Noise model: assume that noise variance is proportional to the elapsed time. noisy_transforms contains the
        # pose-to-pose transforms except with noise applied.
        noisy_transforms = np.zeros((true_poses.shape[0] - 1, 4, 4))
        odom_noise_x = self._odometry_noise_var[GraphGenerator.OdomNoiseDims.X]
        odom_noise_y = self._odometry_noise_var[GraphGenerator.OdomNoiseDims.Y]
        odom_noise_z = self._odometry_noise_var[GraphGenerator.OdomNoiseDims.Z]
        for i in range(0, true_poses.shape[0] - 1):
            this_delta_t = self._odometry_t_vec[i + 1] - self._odometry_t_vec[i]
            noise_as_transform = np.zeros((4, 4))
            noise_as_transform[3, 3] = 1
            noise_as_transform[:3, 3] = np.array([
                np.random.normal(0, np.sqrt(this_delta_t * odom_noise_x)),
                np.random.normal(0, np.sqrt(this_delta_t * odom_noise_y)),
                np.random.normal(0, np.sqrt(this_delta_t * odom_noise_z))
            ])
            theta = np.random.normal(0, np.sqrt(this_delta_t *
                                                self._odometry_noise_var[GraphGenerator.OdomNoiseDims.RVert]))
            # Interpret rotational noise as noise w.r.t. the rotation about the phone's vertical (x) axis
            noise_as_transform[:3, :3] = np.array([
                [1, 0, 0],
                [0, np.cos(theta), -np.sin(theta)],
                [0, np.sin(theta), np.cos(theta)]
            ])
            noisy_transforms[i, :, :] = np.matmul(pose_to_pose[i, :, :], noise_as_transform)

        # Reconstruct new list of poses from the noisy pose-to-pose transforms (dead-reckon, where the initial pose is
        # the same as the true initial pose)
        noisy_poses = np.zeros(true_poses.shape)
        noisy_poses[0, :, :] = true_poses[0, :, :]
        for i in range(0, true_poses.shape[0] - 1):
            noisy_poses[i + 1, :, :] = np.matmul(noisy_poses[i, :, :], noisy_transforms[i, :, :])
        return noisy_poses

    def _get_tag_observation(self, obs_from: np.ndarray, tag: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        """
        Args:
            obs_from: Pose (4x4 homogenous transform) from which the observation is made. Assumed to be from the same
             reference frame as the tag's pose.
            tag: Pose (4x4 homogenous transform) of the tag. Assumed to be from the same reference frame as the obs_from
             pose.

        Returns:
            If the tag is visible: A tuple containing (1) a 4x4 homogenous transform giving the tag pose in the
            reference frame of the phone and (2) a 2x4 array where each column gives the x-y pixel coordinates of the
            pixel projection for a given corner (corner order determined by the TAG_CORNERS_SIZE_2 class attribute).
            Otherwise, two empty arrays are returned.
        """
        tag_in_phone = np.matmul(np.linalg.inv(obs_from), tag)
        if tag_in_phone[2, 3] > 0:  # True if the tag is behind the camera
            return np.array([]), np.array([])

        vector_phone_to_tag = tag_in_phone[:3, 3]
        dist_to_tag = np.linalg.norm(vector_phone_to_tag)
        if dist_to_tag > self._dist_threshold:
            return np.array([]), np.array([])

        # Calculate aoa in the range [0, pi] rad. The angle of attack is found by computing the arccos of
        # normalized vector_phone_to_tag vector and the optical axis of the phone's camera (which is the negative z-axis
        # basis vector of the rotation matrix in the tag_in_phone transform)
        dot_for_aoa = -np.dot(vector_phone_to_tag, tag_in_phone[:3, 2])
        if dist_to_tag == 0:
            # Avoid divide by zero by setting angle of attack to pi
            aoa = np.pi
        else:
            aoa = np.arccos(dot_for_aoa / dist_to_tag) if dot_for_aoa > 0 else \
                (np.pi - np.arccos(dot_for_aoa / dist_to_tag))
        if aoa > self._aoa_threshold:
            return np.array([]), np.array([])

        tag_pixels = np.zeros((2, 4))
        if not self.observe_tag_by_pixels(tag_in_phone, tag_pixels):
            return np.array([]), np.array([])

        return tag_in_phone, tag_pixels

    def observe_tag_by_pixels(self, tag_in_phone, pixel_vals: np.ndarray) -> bool:
        """Project points in 3D space into phone space.

        Args:
            tag_in_phone: Transform of the tag observation in the phone's reference frame. This array is modified such
             that it contains the new tag transform after the coordinate projection, noise addition, and pose
             re-computation
            pixel_vals: 2x4 array populated with coordinates of the tag observation after noise is applied.
        Returns:
            True if all points are visible according to the camera intrinsics.
        """
        # Flip the y and z because the math for the camera intrinsics assumes that +z is increasing depth from the
        # camera (and the AR kit has +z facing out of the screen).
        flipped_tag_in_phone = np.matmul(FLIP_Y_AND_Z_AXES, tag_in_phone)
        tag_corners_in_flipped_phone = np.matmul(flipped_tag_in_phone, self._tag_corners_in_tag)

        # 3xN array of N points' pixel coordinates (accurate only after subsequent normalization)
        pixel_coords_flipped = np.matmul(GraphGenerator.CAMERA_INTRINSICS, tag_corners_in_flipped_phone[:3, :])

        # Normalize values so that first and second rows contain the actual pixel values
        for col_idx in range(pixel_coords_flipped.shape[1]):
            if pixel_coords_flipped[2, col_idx] == 0:
                # Avoid divide-by-zero by setting pixels to -1 (corresponds to non-visible observation)
                pixel_coords_flipped[:, col_idx] = -1
            pixel_coords_flipped[:, col_idx] = pixel_coords_flipped[:, col_idx] / pixel_coords_flipped[2, col_idx]

        pixel_vals[:, :] = pixel_coords_flipped[:2, :]

        # Check if all pixel coordinates are within the sensor's bounds
        if np.any(pixel_coords_flipped[0:2, :] < 0) or \
                np.any(pixel_coords_flipped[0, :] > GraphGenerator._double_camera_intrinsics[0, 2]) or \
                np.any(pixel_coords_flipped[1, :] > GraphGenerator._double_camera_intrinsics[1, 2]):
            return False

        # Now, we need the tag_in_phone transform to use the OpenCV reference frame convention
        opencv_tag_in_phone = np.matmul(np.linalg.inv(AR_TO_OPENCV), tag_in_phone)
        tag_corners_in_opencv_phone = np.matmul(opencv_tag_in_phone, self._tag_corners_in_tag)

        # 3xN array of N points' pixel coordinates (accurate only after subsequent normalization)
        pixel_coords_opencv = np.matmul(GraphGenerator.CAMERA_INTRINSICS, tag_corners_in_opencv_phone[:3, :])

        # Normalize values so that first and second rows contain the actual pixel values
        for col_idx in range(pixel_coords_opencv.shape[1]):
            if pixel_coords_opencv[2, col_idx] == 0:
                # Avoid divide-by-zero by setting pixels to -1 (corresponds to non-visible observation)
                pixel_coords_opencv[:, col_idx] = -1
            pixel_coords_opencv[:, col_idx] = pixel_coords_opencv[:, col_idx] / pixel_coords_opencv[2, col_idx]

        opencv_pixel_vals = pixel_coords_opencv[:2, :] + np.random.randn(2, 4) * self._obs_noise_var
        _, r_vec, t_vec = cv2.solvePnP(self._tag_corners_for_pnp, np.transpose(opencv_pixel_vals),
                                       self.CAMERA_INTRINSICS, None, flags=cv2.SOLVEPNP_IPPE_SQUARE)
        if np.isnan(np.sum(r_vec)):
            return False  # TODO: figure out why NaN numbers show up sometimes
        rot_mat, _ = cv2.Rodrigues(r_vec)

        new_transform = np.zeros((4, 4))
        new_transform[:3, :3] = rot_mat.transpose()
        new_transform[:3, 3] = t_vec.transpose()
        new_transform[3, 3] = 1
        new_transform[:, :] = np.matmul(AR_TO_OPENCV, new_transform)  # Undo the conversion to the OpenCV frame
        tag_in_phone[:, :] = new_transform
        return True

    # -- Static methods --

    @staticmethod
    def xz_path_ellipsis_four_by_two(t_vec: np.ndarray, path_args: Dict[str, Union[float, Tuple[float, float]]]) \
            -> np.ndarray:
        """Defines a parameterized path that is a counterclockwise ellipses in a plane co-planar to the xz plane.

        Args:
            t_vec: N-length vector of parameters to evaluate the curve at
            path_args: Expects a dictionary containing the keys "e_xw", "e_zw", "e_cp", and "xzp" whose values define
             the ellipse's width in the x-direction, width in the z-direction, centerpoint, and y-value of the plane of
             the path respectively.

        Returns:
            3xN array where the rows from top to bottom are the x, y, and z coordinates respectively.

        Raises:
            ValueError: If the path_args dictionary does not contain the expected keys.
        """
        try:
            cp: Tuple[float, float] = path_args["e_cp"]
            return np.vstack((-(path_args["e_xw"] / 2) * np.sin(t_vec) + cp[0],
                              np.ones(len(t_vec), ) * path_args["xzp"],
                              -(path_args["e_zw"] / 2) * np.cos(t_vec) + cp[1]))
        except KeyError:
            raise ValueError("path_args argument did not contain the expected keys 'e_xw', 'e_zw', 'e_cp', and 'xzp' "
                             "for an elliptical path")

    # noinspection PyUnresolvedReferences
    PARAMETERIZED_PATH_ALIAS_TO_CALLABLE: Dict[str, Callable[[np.ndarray, Dict[str, float]], np.ndarray]] = {
        "e": xz_path_ellipsis_four_by_two.__func__
    }

    @staticmethod
    def draw_frames(offsets: np.ndarray, frames: np.ndarray, plt_axes: plt.Axes,
                    colors: Tuple[str, str, str] = ("r", "g", "b")) -> None:
        """Draw N reference frames at given translation offsets.

        Args:
            offsets: Translation offsets of the frames. Expected to be a 3xN matrix where the rows from top to bottom
             encode the translation offset in the first, second, and third dimensions, respectively.
            frames: Nx3x3 array of rotation matrices.
            plt_axes: Matplotlib axes to plot on
            colors: Tuple of color codes to use for the first, second, and third dimensions' basis vector arrows,
             respectively.
        """
        for b in range(3):
            basis_vectors_transposed = (frames[:, :, b]).transpose()
            plt_axes.quiver(
                offsets[0, :],
                offsets[1, :],
                offsets[2, :],
                # For each basis vector, dot it with the corresponding basis vector of the reference frame it is within
                np.matmul(GraphGenerator.X_HAT_1X3, basis_vectors_transposed),
                np.matmul(GraphGenerator.Y_HAT_1X3, basis_vectors_transposed),
                np.matmul(GraphGenerator.Z_HAT_1X3, basis_vectors_transposed),
                length=0.5,
                arrow_length_ratio=0.3,
                normalize=True,
                color=colors[b],
            )

    @staticmethod
    def frenet_frames(t_vec: np.ndarray,
                      ftg: Callable[[np.ndarray, Dict[str, Union[float, Tuple[float, float]]]], np.ndarray],
                      path_args: Dict[str, Union[float, Tuple[float, float]]]) -> np.ndarray:
        """Computes the provided curve's Frenet frames' basis vectors at given points in time.

        Args:
            t_vec: N-length Vector of parameters to evaluate the curve at
            ftg: Function defining the Frenet frame's position in the global reference frame.
            path_args: Value to be passed as the path_args argument for the path invocation

        Returns:
            Nx3x3 numpy array where, for each 3x3 sub-array, the columns from left to right are the T, N, and B basis
             vectors of unit magnitude.
        """
        t_hat = GraphGenerator.d_curve_dt(t_vec, GraphGenerator.PATH_LINEAR_DELTA_T, ftg, path_args)  # 3xN
        t_hat = norm_array_cols(t_hat)

        # Approximate the derivative of t_hat to get n_hat
        dt_div_2 = GraphGenerator.PATH_LINEAR_DELTA_T / 2
        t_hat_dt_upper = GraphGenerator.d_curve_dt(t_vec + dt_div_2, GraphGenerator.PATH_LINEAR_DELTA_T, ftg, path_args)
        t_hat_dt_upper = norm_array_cols(t_hat_dt_upper)
        t_hat_dt_lower = GraphGenerator.d_curve_dt(t_vec - dt_div_2, GraphGenerator.PATH_LINEAR_DELTA_T, ftg, path_args)
        t_hat_dt_lower = norm_array_cols(t_hat_dt_lower)
        n_hat = (t_hat_dt_upper - t_hat_dt_lower) / GraphGenerator.PATH_LINEAR_DELTA_T
        n_hat = norm_array_cols(n_hat)

        # Compute b_hat through the cross product
        b_hat = np.cross(t_hat, n_hat, axis=0)

        ret = np.zeros((len(t_vec), 3, 3))
        ret[:, :, 0] = t_hat.transpose()
        ret[:, :, 1] = n_hat.transpose()
        ret[:, :, 2] = b_hat.transpose()
        return ret

    @staticmethod
    def d_curve_dt(t_vec: np.ndarray, dt: float,
                   path: Callable[[np.ndarray, Dict[str, Union[float, Tuple[float, float]]]], np.ndarray],
                   path_args: Dict[str, Union[float, Tuple[float, float]]]) -> np.ndarray:
        """Approximates the derivative of a parameterized curve (using the centralized method) with respect to the
        parameter.

        Args:
            t_vec: N-length Vector of parameters to evaluate the curve at
            dt: Parameter delta used for the linear approximation.
            path: Parameterized curve in 3 dimensions.
            path_args: Value to be passed as the path_args argument for the path invocation

        Returns:
            3xN array giving the derivative in each dimension of the curve.
        """
        return (path(t_vec + dt / 2, path_args) - path(t_vec - dt / 2, path_args)) / dt
