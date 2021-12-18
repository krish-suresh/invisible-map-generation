"""
Utility functions for graph optimization
"""

import json
import math
from typing import Union, List, Dict, Optional

import g2o
import numpy as np
from g2o import SE3Quat, EdgeProjectPSI2UV, EdgeSE3Expmap, EdgeSE3

from . import graph_util_get_neighbors
from .graph_vertex_edge_classes import VertexType


class Weights:
    def __init__(self, odometry: Optional[np.ndarray] = None, tag: Optional[np.ndarray] = None,
                 tag_sba: Optional[np.ndarray] = None, dummy: Optional[np.ndarray] = None,
                 odom_tag_ratio: Optional[Union[np.ndarray, float]] = None):
        self.dummy: np.ndarray = np.array(dummy) if dummy is not None else np.ones(3)
        self.odometry: np.ndarray = np.array(odometry) if odometry is not None else np.ones(6)
        self.tag: np.ndarray = np.array(tag) if tag is not None else np.ones(6)
        self.tag_sba: np.ndarray = np.array(tag_sba) if tag is not None else np.ones(2)

        # Put lower limit of 0.00001 to prevent rounding causing division by 0
        self.odom_tag_ratio: float
        if isinstance(odom_tag_ratio, float):
            self.odom_tag_ratio = max(0.00001, odom_tag_ratio)
        elif isinstance(odom_tag_ratio, np.ndarray):
            self.odom_tag_ratio = max(0.00001, odom_tag_ratio[0])
        else:
            self.odom_tag_ratio = 1
        # self.normalize_tag_and_odom_weights()

    @property
    def tag_odom_ratio(self):
        return 1 / self.odom_tag_ratio

    @classmethod
    def legacy_from_array(cls, array: Union[np.ndarray, List[float]]) -> "Weights":
        return cls.legacy_from_dict(cls.weight_dict_from_array(array))

    @classmethod
    def legacy_from_dict(cls, dct: Dict[str, Union[np.ndarray, float]]) -> "Weights":
        return cls(**dct)

    def to_dict(self) -> Dict[str, Union[float, np.ndarray]]:
        return {
            "dummy": np.array(self.dummy),
            "odometry": np.array(self.odometry),
            "tag": np.array(self.tag),
            "tag_sba": np.array(self.tag_sba),
            "odom_tag_ratio": self.odom_tag_ratio
        }

    @staticmethod
    def weight_dict_from_array(array: Union[np.ndarray, List[float]]) -> Dict[str, Union[float, np.ndarray]]:
        """
        Constructs a normalized weight dictionary from a given array of values
        """
        weights = {
            'dummy': np.array([-1, 1e2, -1]),
            'odometry': np.ones(6),
            'tag': np.ones(6),
            'tag_sba': np.ones(2),
            'odom_tag_ratio': 1
        }

        length = array.size if isinstance(array, np.ndarray) else len(array)
        half_len = length // 2
        has_ratio = length % 2 == 1

        if length == 1:  # ratio
            weights['odom_tag_ratio'] = array[0]
        elif length == 2:  # tag/odom pose:rot/tag-sba x:y, ratio
            weights['odometry'] = np.array([array[0]] * 3 + [1] * 3)
            weights['tag'] = np.array([array[0]] * 3 + [1] * 3)
            weights['tag_sba'] = np.array([array[0], 1])
            weights['odom_tag_ratio'] = array[1]
        elif length == 3:  # odom pose:rot, tag pose:rot/tag-sba x:y, ratio
            weights['odometry'] = np.array([array[0]] * 3 + [1] * 3)
            weights['tag'] = np.array([array[1]] * 3 + [1] * 3)
            weights['tag_sba'] = np.array([array[1], 1])
            weights['odom_tag_ratio'] = array[2]
        elif half_len == 2:  # odom pose, odom rot, tag pose/tag-sba x, tag rot/tag-sba y, (ratio)
            weights['odometry'] = np.array([array[0]] * 3 + [array[1]] * 3)
            weights['tag'] = np.array([array[2]] * 3 + [array[3]] * 3)
            weights['tag_sba'] = np.array(array[2:])
            weights['odom_tag_ratio'] = array[-1] if has_ratio else 1
        elif half_len == 3:  # odom x y z qx qy, tag-sba x, (ratio)
            weights['odometry'] = np.array(array[:5])
            weights['tag_sba'] = np.array([array[5]])
            weights['odom_tag_ratio'] = array[-1] if has_ratio else 1
        elif length == 4:  # odom, tag-sba, (ratio)
            weights['odometry'] = np.array(array[:6])
            weights['tag_sba'] = np.array(array[6:])
            weights['odom_tag_ratio'] = array[-1] if has_ratio else 1
        elif length == 5:  # odom x y z qx qy, tag x y z qx qy, (ratio)
            weights['odometry'] = np.array(array[:5])
            weights['tag'] = np.array(array[5:])
            weights['odom_tag_ratio'] = array[-1] if has_ratio else 1
        elif length == 6:  # odom, tag, (ratio)
            weights['odometry'] = np.array(array[:6])
            weights['tag'] = np.array(array[6:])
            weights['odom_tag_ratio'] = array[-1] if has_ratio else 1
        else:
            raise Exception(f'Weight length of {length} is not supported')

        w = Weights.legacy_from_dict(weights)
        w.normalize_tag_and_odom_weights()
        return w.to_dict()

    def normalize_tag_and_odom_weights(self):
        """Normalizes the tag and odometry weights' magnitudes, then applies the odom-to-tag ratio as a scaling factor.
        """
        odom_mag = np.linalg.norm(self.odometry)
        if odom_mag == 0:  # Avoid divide by zero error
            odom_mag = 1
        self.odometry *= self.odom_tag_ratio / odom_mag

        # TODO: The below implements what was previously in place for SBA weighting. Should it be changed? Why is
        #  such a low weighting so effective?
        sba_mag = np.linalg.norm(self.tag_sba)
        if sba_mag == 0:
            sba_mag = 1  # Avoid divide by zero error
        self.tag_sba *= 1 / (sba_mag * 1464)

        tag_mag = np.linalg.norm(self.tag)
        if tag_mag == 0:  # Avoid divide by zero error
            tag_mag = 1
        self.tag *= 1 / tag_mag


def optimizer_to_map(vertices, optimizer: g2o.SparseOptimizer, is_sba=False) -> Dict[str, Union[List, np.ndarray]]:
    """Convert a :class: g2o.SparseOptimizer to a dictionary containing locations of the phone, tags, and waypoints.

    Args:
        vertices: A dictionary of vertices. This is used to look up the type of vertex pulled from the optimizer.
        optimizer: a :class: g2o.SparseOptimizer containing a map.
        is_sba: Set to True if the optimizer is based on sparse bundle adjustment and False
         otherwise. If true, the odometry locations and the tag vertices' poses are inverted. In the case of the tag
         vertices, the poses are first transformed by a -1 translation (applied on the LHS of the pose) before
         inversion.

    Returns:
        A dictionary with fields 'locations', 'tags', and 'waypoints'. The 'locations' key covers a (n, 8) array
         containing x, y, z, qx, qy, qz, qw locations of the phone as well as the vertex uid at n points. The 'tags' and
        'waypoints' keys cover the locations of the tags and waypoints in the same format.
    """
    locations = []
    tagpoints = []
    tags = []
    waypoints = []
    waypoint_metadata = []
    exaggerate_tag_corners = True
    for i in optimizer.vertices():
        mode = vertices[i].mode
        if mode == VertexType.TAGPOINT:
            tag_vert = optimizer_find_connected_tag_vert(optimizer, optimizer.vertex(i))
            if tag_vert is None:
                # TODO: double-check that the right way to handle this case is to continue
                continue
            location = optimizer.vertex(i).estimate()
            if exaggerate_tag_corners:
                location = location * np.array([10, 10, 1])
            tagpoints.append(tag_vert.estimate().inverse() * location)
        else:
            location = optimizer.vertex(i).estimate().translation()
            rotation = optimizer.vertex(i).estimate().rotation().coeffs()
            pose = np.concatenate([location, rotation])

            if mode == VertexType.ODOMETRY:
                if is_sba:
                    pose = SE3Quat(pose).inverse().to_vector()
                pose_with_metadata = np.concatenate([pose, [i], [vertices[i].meta_data['pose_id']]])
                locations.append(pose_with_metadata)
            elif mode == VertexType.TAG:
                pose_with_metadata = np.concatenate([pose, [i]])
                if is_sba:
                    # Adjust tag based on the position of the tag center
                    pose_with_metadata[:-1] = (SE3Quat([0, 0, -1, 0, 0, 0, 1]) * SE3Quat(pose)).inverse().to_vector()
                if 'tag_id' in vertices[i].meta_data:
                    pose_with_metadata[-1] = vertices[i].meta_data['tag_id']
                tags.append(pose_with_metadata)
            elif mode == VertexType.WAYPOINT:
                pose_with_metadata = np.concatenate([pose, [i]])
                waypoints.append(pose_with_metadata)
                waypoint_metadata.append(vertices[i].meta_data)
    locations_arr = np.array(locations)
    locations_arr = locations_arr[locations_arr[:, -1].argsort()] if len(locations) > 0 else np.zeros((0, 9))
    tags_arr = np.array(tags) if len(tags) > 0 else np.zeros((0, 8))
    tagpoints_arr = np.array(tagpoints) if len(tagpoints) > 0 else np.zeros((0, 3))
    waypoints_arr = np.array(waypoints) if len(waypoints) > 0 else np.zeros((0, 8))
    return {'locations': locations_arr, 'tags': tags_arr, 'tagpoints': tagpoints_arr,
            'waypoints': [waypoint_metadata, waypoints_arr]}


def optimizer_to_map_chi2(graph, optimizer: g2o.SparseOptimizer, is_sba=False) -> \
        Dict[str, Union[List, np.ndarray]]:
    """Convert a :class: g2o.SparseOptimizer to a dictionary containing locations of the phone, tags, waypoints, and
    per-odometry edge chi2 information.

    This function works by calling `optimizer_to_map` and adding a new entry that is a vector of the per-odometry edge
    chi2 information as calculated by the `map_odom_to_adj_chi2` method of the `Graph` class.

    Args:
        graph (Graph): A graph instance whose vertices attribute is passed as the first argument to `optimizer_to_map`
         and whose `map_odom_to_adj_chi2` method is used.
        optimizer: a :class: g2o.SparseOptimizer containing a map, which is passed as the second argument to
         `optimizer_to_map`.
        is_sba: True if the optimizer is based on sparse bundle adjustment and False otherwise;
         passed as the `is_sba` keyword argument to `optimizer_to_map`.

    Returns:
        A dictionary with fields 'locations', 'tags', 'waypoints', and 'locationsAdjChi2'. The 'locations' key covers a
        (n, 8) array  containing x, y, z, qx, qy, qz, qw locations of the phone as well as the vertex uid at n points.
        The 'tags' and 'waypoints' keys cover the locations of the tags and waypoints in the same format. Associated
        with each odometry node is a chi2 calculated from the `map_odom_to_adj_chi2` method of the `Graph` class, which
        is stored in the vector in the locationsAdjChi2 vector.
    """
    ret_map = optimizer_to_map(graph.vertices, optimizer, is_sba=is_sba)
    locations_shape = np.shape(ret_map["locations"])
    locations_adj_chi2 = np.zeros([locations_shape[0], 1])
    visible_tags_count = np.zeros([locations_shape[0], 1])

    for i, odom_node_vec in enumerate(ret_map["locations"]):
        uid = round(odom_node_vec[7])  # UID integer is stored as a floating point number, so cast it to an integer
        locations_adj_chi2[i], visible_tags_count[i] = graph.map_odom_to_adj_chi2(uid)

    ret_map["locationsAdjChi2"] = locations_adj_chi2
    ret_map["visibleTagsCount"] = visible_tags_count
    return ret_map


def optimizer_find_connected_tag_vert(optimizer: g2o.SparseOptimizer, location_vert):
    """TODO: documentation
    """
    # TODO: it would be nice if we didn't have to scan the entire graph
    for edge in optimizer.edges():
        if type(edge) == EdgeProjectPSI2UV:
            if edge.vertex(0).id() == location_vert.id():
                return edge.vertex(2)
    return None


def get_chi2_of_edge(edge: Union[EdgeProjectPSI2UV, EdgeSE3Expmap, EdgeSE3]) -> float:
    """Computes the chi2 value associated with the provided edge

    Arguments:
        edge (Union[EdgeProjectPSI2UV, EdgeSE3Expmap, EdgeSE3]): A g2o edge

    Returns:
        Chi2 value associated with the provided edge

    Raises:
        Exception if an edge is encountered that is not handled (handled edges are EdgeProjectPSI2UV,
         EdgeSE3Expmap, and EdgeSE3)
    """
    if isinstance(edge, EdgeProjectPSI2UV):
        # Based on this function: https://github.com/uoip/g2opy/blob/5587024b17fd812c66d91740716fbf0bf5824fbc/g2o/types/
        #  sba/types_six_dof_expmap.cpp#L174
        cam = edge.parameter(0)
        camera_coords = edge.vertex(1).estimate() * edge.vertex(2).estimate().inverse() * edge.vertex(0).estimate()
        pixel_coords = cam.cam_map(camera_coords)
        error = edge.measurement() - pixel_coords
        return error.dot(edge.information()).dot(error)
    elif isinstance(edge, EdgeSE3Expmap):
        error = edge.vertex(1).estimate().inverse() * edge.measurement() * edge.vertex(0).estimate()
        chi2 = error.log().T.dot(edge.information()).dot(error.log())
        # noinspection SpellCheckingInspection
        if math.isnan(chi2):
            # print(f'vertex 0 estimate:\n{edge.vertex(0).estimate().matrix()}'
            #       f'\nfull:\n{edge.vertex(0).estimate().adj()}')
            # print(f'vertex 1 estimate:\n{edge.vertex(1).estimate().matrix()}'
            #       f'\nfull:\n{edge.vertex(1).estimate().adj()}')
            # print(f'transform_vector:\n{edge.transform_vector().matrix()}\nfull:\n{edge.transform_vector().adj()}')
            # print(f'calc. error:\n{error.matrix()}\nfull:{error.adj()}')
            # print(f'omega:\n{edge.information()}')
            # if weights is not None:
            #     print(f'weights:\n{weights}')
            raise Exception('chi2 is NaN for an edge of type EdgeSE3Expmap')
        return chi2
    elif isinstance(edge, EdgeSE3):
        delta = edge.measurement().inverse() * edge.vertex(0).estimate().inverse() * edge.vertex(1).estimate()
        error = np.hstack((delta.translation(), delta.orientation().coeffs()[:-1]))
        return error.dot(edge.information()).dot(error)
    else:
        raise Exception("Unhandled edge type for chi2 calculation")


def sum_optimized_edges_chi2(optimizer: g2o.SparseOptimizer, verbose: bool = True) -> float:
    """Iterates through edges in the g2o sparse optimizer object and sums the chi2 values for all the edges.

    Args:
        optimizer: A SparseOptimizer object
        verbose (bool): Boolean for whether to print the total chi2 value

    Returns:
        Sum of the chi2 values associated with each edge
    """
    total_chi2 = 0.0
    for edge in optimizer.edges():
        total_chi2 += get_chi2_of_edge(edge)

    if verbose:
        print("Total chi2:", total_chi2)

    return total_chi2


def ground_truth_metric(optimized_tag_verts: np.ndarray, ground_truth_tags: np.ndarray, verbose: bool = False) \
        -> float:
    """Error metric for tag pose accuracy.

    Calculates the transforms from the anchor tag to each other tag for the optimized and the ground truth tags,
    then compares the transforms and finds the difference in the translation components.

    Args:
        optimized_tag_verts: A n-by-7 numpy array containing length-7 pose vectors.
        ground_truth_tags: A n-by-7 numpy array containing length-7 pose vectors.
        verbose: A boolean representing whether to print the full comparisons for each tag.

    Returns:
        A float representing the average difference in tag positions (translation only) in meters.
    """
    num_tags = optimized_tag_verts.shape[0]
    sum_trans_diffs = np.zeros((num_tags,))
    ground_truth_as_se3 = [SE3Quat(tag_pose) for tag_pose in ground_truth_tags]

    for anchor_tag in range(num_tags):
        anchor_tag_se3quat = SE3Quat(optimized_tag_verts[anchor_tag])
        to_world: SE3Quat = anchor_tag_se3quat * SE3Quat(ground_truth_tags[anchor_tag]).inverse()
        world_frame_ground_truth = np.asarray([(to_world * tag).to_vector() for tag in ground_truth_as_se3])[:, :3]
        sum_trans_diffs += np.linalg.norm(world_frame_ground_truth - optimized_tag_verts[:, :3], axis=1)
    avg_trans_diffs = sum_trans_diffs / num_tags
    avg = float(np.mean(avg_trans_diffs))
    if verbose:
        print(f'Ground truth metric is {avg}')
    return avg


def make_processed_map_JSON(opt_result: Dict[str, Union[List, np.ndarray]], calculate_intersections: bool = False) \
        -> str:
    """Serializes the result of an optimization into a JSON that is of an acceptable format for uploading to the
    database.

    Args:
        opt_result: A dictionary containing the tag locations, odometry locations, waypoint locations,
         odometry-adjacent chi2 array, and per-odometry-node visible tags count array in the keys 'tags', 'locations',
         'waypoints', 'locationsAdjChi2', and 'visibleTagsCount', respectively. This is the format of dictionary that is
         produced by the `map_processing.graph_opt_utils.optimizer_to_map_chi2` function and, subsequently, the
         `GraphManager.optimize_graph` method.
        calculate_intersections: If true, graph_util_get_neighbors.get_neighbors is called with the odometry nodes
         as the argument. The results are appended to the resulting tag vertex map under the 'neighbors' key.

    Returns:
        Json string containing the serialized results.
    """
    tag_locations = opt_result["tags"]
    odom_locations = opt_result["locations"]
    waypoint_locations = tuple(opt_result["waypoints"])
    adj_chi2_arr = opt_result["locationsAdjChi2"]
    visible_tags_count = opt_result["visibleTagsCount"]

    if (visible_tags_count is None) ^ (adj_chi2_arr is None):
        print("visible_tags_count and adj_chi2_arr arguments must both be None or non-None")

    tag_vertex_map = map(
        lambda curr_tag: {
            "translation": {"x": curr_tag[0],
                            "y": curr_tag[1],
                            "z": curr_tag[2]},
            "rotation": {"x": curr_tag[3],
                         "y": curr_tag[4],
                         "z": curr_tag[5],
                         "w": curr_tag[6]},
            "id": int(curr_tag[7])
        },
        tag_locations
    )

    odom_vertex_map: List[Dict[str, Union[List[int], int, float, Dict[str, float]]]]
    if adj_chi2_arr is None:
        odom_vertex_map = list(
            map(
                lambda curr_odom: {
                    "translation": {"x": curr_odom[0],
                                    "y": curr_odom[1],
                                    "z": curr_odom[2]},
                    "rotation": {"x": curr_odom[3],
                                 "y": curr_odom[4],
                                 "z": curr_odom[5],
                                 "w": curr_odom[6]},
                    "poseId": int(curr_odom[8]),
                },
                odom_locations
            )
        )
    else:
        odom_locations_with_chi2_and_viz_tags = np.concatenate(
            [odom_locations, adj_chi2_arr, visible_tags_count],
            axis=1
        )
        odom_vertex_map = list(
            map(
                lambda curr_odom: {
                    "translation": {"x": curr_odom[0],
                                    "y": curr_odom[1],
                                    "z": curr_odom[2]},
                    "rotation": {"x": curr_odom[3],
                                 "y": curr_odom[4],
                                 "z": curr_odom[5],
                                 "w": curr_odom[6]},
                    "poseId": int(curr_odom[8]),
                    "adjChi2": curr_odom[9],
                    "vizTags": curr_odom[10]
                },
                odom_locations_with_chi2_and_viz_tags
            )
        )

    if calculate_intersections:
        neighbors, intersections = graph_util_get_neighbors.get_neighbors(odom_locations[:, :7])
        for index, neighbor in enumerate(neighbors):
            odom_vertex_map[index]["neighbors"] = neighbor
        for intersection in intersections:
            odom_vertex_map.append(intersection)

    waypoint_vertex_map = map(
        lambda idx: {
            "translation": {"x": waypoint_locations[1][idx][0],
                            "y": waypoint_locations[1][idx][1],
                            "z": waypoint_locations[1][idx][2]},
            "rotation": {"x": waypoint_locations[1][idx][3],
                         "y": waypoint_locations[1][idx][4],
                         "z": waypoint_locations[1][idx][5],
                         "w": waypoint_locations[1][idx][6]},
            "id": waypoint_locations[0][idx]["name"]
        }, range(len(waypoint_locations[0]))
    )
    return json.dumps({"tag_vertices": list(tag_vertex_map),
                       "odometry_vertices": odom_vertex_map,
                       "waypoints_vertices": list(waypoint_vertex_map)}, indent=2)


def compare_std_dev(all_tags, all_tags_original):
    """TODO: documentation
    """
    return {int(tag_id): (np.std(all_tags_original[all_tags_original[:, -1] == tag_id, :-1], axis=0),
                          np.std(all_tags[all_tags[:, -1] == tag_id, :-1], axis=0)) for tag_id in
            np.unique(all_tags[:, -1])}
