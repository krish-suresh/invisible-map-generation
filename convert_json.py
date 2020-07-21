import numpy as np
from scipy.spatial.transform import Rotation as R

import graph


def pose2diffs(poses):
    """Convert an array of poses in the odom frame to an array of
    transformations from the last pose.

    Args:
      poses (np.ndarray): Pose or array of poses.
    Returns:
      An array of transformations
    """
    diffs = []
    for previous_pose, current_pose in zip(poses[:-1], poses[1:]):
        diffs.append(np.linalg.inv(previous_pose).dot(current_pose))
    diffs = np.array(diffs)
    return diffs


def matrix2measurement(pose):
    """ Convert a pose or array of poses in matrix form to [x, y, z,
    qx, qy, qz, qw].

    The output will have one fewer dimension than the input.

    Args:
      pose (np.ndarray): Pose or array of poses in matrix form.
        The poses are converted along the last two axes.
    Returns:
      Converted pose or array of poses.
    """
    translation = pose[..., :3, 3]
    rotation = R.from_matrix(pose[..., :3, :3]).as_quat()
    return np.concatenate([translation, rotation], axis=-1)


def as_graph(dct):
    """Convert a dictionary decoded from JSON into a graph.

    Args:
      dct (dict): The dictionary to convert to a graph.
    Returns:
      A graph derived from the input dictionary.
    """
    pose_data = np.array(dct['pose_data'])
    if not pose_data.size:
        pose_data = np.zeros((0, 18))
    pose_matrices = pose_data[:, :16].reshape(-1, 4, 4).transpose(0, 2, 1)
    odom_vertex_estimates = matrix2measurement(pose_matrices)

    # The camera axis used to get tag measurements are flipped
    # relative to the phone frame used for odom measurements
    camera_to_odom_transform = np.array([
        [0, 1, 0, 0],
        [1, 0, 0, 0],
        [0, 0, -1, 0],
        [0, 0, 0, 1]
    ])
    tag_list_uniform = list(map(lambda x: np.asarray(x).reshape((-1, 19)), dct['tag_data']))
    if tag_list_uniform:
        tag_data_uniform = np.concatenate(tag_list_uniform)
    else:
        tag_data_uniform = np.zeros((0,19))
    tag_edge_measurements_matrix = np.matmul(
        camera_to_odom_transform, tag_data_uniform[:, 1:17].reshape(-1, 4, 4))
    tag_edge_measurements = matrix2measurement(tag_edge_measurements_matrix)

    unique_tag_ids = np.unique(tag_data_uniform[:, 0]).astype('i')
    tag_vertex_id_by_tag_id = dict(
        zip(unique_tag_ids, range(unique_tag_ids.size)))

    # Enable lookup of tags by the frame they appear in
    tag_vertex_id_and_index_by_frame_id = {}

    for tag_index, (tag_id, tag_frame) in enumerate(tag_data_uniform[:, [0, 18]]):
        tag_vertex_id = tag_vertex_id_by_tag_id[tag_id]
        tag_vertex_id_and_index_by_frame_id[tag_frame] = tag_vertex_id_and_index_by_frame_id.get(
            tag_frame, [])
        tag_vertex_id_and_index_by_frame_id[tag_frame].append(
            (tag_vertex_id, tag_index))

    waypoint_list_uniform = list(map(lambda x: np.asarray(x[:-1]).reshape((-1, 18)), dct.get('location_data', [])))
    waypoint_names = list(map(lambda x: x[-1], dct.get('location_data', [])))
    unique_waypoint_names = np.unique(waypoint_names)
    if waypoint_list_uniform:
        waypoint_data_uniform = np.concatenate(waypoint_list_uniform)
    else:
        waypoint_data_uniform = np.zeros((0,18))
    waypoint_edge_measurements_matrix = waypoint_data_uniform[:, :16].reshape(-1, 4, 4)
    waypoint_edge_measurements = matrix2measurement(waypoint_edge_measurements_matrix)

    waypoint_vertex_id_by_name = dict(
        zip(unique_waypoint_names, range(unique_tag_ids.size, unique_tag_ids.size + unique_waypoint_names.size)))
    waypoint_name_by_vertex_id = dict(zip(waypoint_vertex_id_by_name.values(), waypoint_vertex_id_by_name.keys()))
    # Enable lookup of waypoints by the frame they appear in
    waypoint_vertex_id_and_index_by_frame_id = {}

    for waypoint_index, (waypoint_name, waypoint_frame) in enumerate(zip(waypoint_names, waypoint_data_uniform[:, 17])):
        waypoint_vertex_id = waypoint_vertex_id_by_name[waypoint_name]
        waypoint_vertex_id_and_index_by_frame_id[waypoint_frame] = waypoint_vertex_id_and_index_by_frame_id.get(
            waypoint_name, [])
        waypoint_vertex_id_and_index_by_frame_id[waypoint_frame].append(
            (waypoint_vertex_id, waypoint_index))

    # Construct the dictionaries of vertices and edges
    vertices = {}
    edges = {}
    vertex_counter = unique_tag_ids.size + unique_waypoint_names.size
    edge_counter = 0

    previous_vertex = None
    previous_pose_matrix = None
    counted_tag_vertex_ids = set()
    counted_waypoint_vertex_ids = set()
    first_odom_processed = False
    num_tag_edges = 0

    for i, odom_frame in enumerate(pose_data[:, 17]):
        current_odom_vertex_uid = vertex_counter
        vertices[current_odom_vertex_uid] = graph.Vertex(
            mode=graph.VertexType.ODOMETRY,
            estimate=odom_vertex_estimates[i],
            fixed=not first_odom_processed
        )
        first_odom_processed = True

        vertex_counter += 1

        # Connect odom to tag vertex
        for tag_vertex_id, tag_index in tag_vertex_id_and_index_by_frame_id.get(int(odom_frame), []):
            if tag_vertex_id not in counted_tag_vertex_ids:
                vertices[tag_vertex_id] = graph.Vertex(
                    mode=graph.VertexType.TAG,
                    estimate=matrix2measurement(pose_matrices[i].dot(
                        tag_edge_measurements_matrix[tag_index])),
                    fixed=False
                )
                counted_tag_vertex_ids.add(tag_vertex_id)

            edges[edge_counter] = graph.Edge(
                startuid=current_odom_vertex_uid,
                enduid=tag_vertex_id,
                information=np.eye(6),
                measurement=tag_edge_measurements[tag_index]
                # measurement=np.array([0, 0, 0, 0, 0, 0, 1])
            )
            num_tag_edges += 1

            edge_counter += 1

        # Connect odom to waypoint vertex
        for waypoint_vertex_id, waypoint_index in waypoint_vertex_id_and_index_by_frame_id.get(int(odom_frame), []):
            if waypoint_vertex_id not in counted_waypoint_vertex_ids:
                vertices[waypoint_vertex_id] = graph.Vertex(
                    mode=graph.VertexType.WAYPOINT,
                    estimate=matrix2measurement(pose_matrices[i].dot(
                        waypoint_edge_measurements_matrix[waypoint_index])),
                    fixed=False
                )
                vertices[waypoint_vertex_id].meta_data['name'] = waypoint_name_by_vertex_id[waypoint_vertex_id]
                counted_waypoint_vertex_ids.add(waypoint_vertex_id)

            edges[edge_counter] = graph.Edge(
                startuid=current_odom_vertex_uid,
                enduid=waypoint_vertex_id,
                information=np.eye(6),
                measurement=waypoint_edge_measurements[waypoint_index]
                # measurement=np.array([0, 0, 0, 0, 0, 0, 1])
            )

            edge_counter += 1

        if previous_vertex:
            edges[edge_counter] = graph.Edge(
                startuid=previous_vertex,
                enduid=current_odom_vertex_uid,
                information=np.eye(6),
                measurement=matrix2measurement(np.linalg.inv(
                    previous_pose_matrix).dot(pose_matrices[i]))
            )
            edge_counter += 1

        # make dummy node
        dummy_node_uid = vertex_counter
        vertices[dummy_node_uid] = graph.Vertex(
            mode=graph.VertexType.DUMMY,
            estimate=np.hstack((np.zeros(3,),odom_vertex_estimates[i][3:])),
            fixed=True
        )
        vertex_counter += 1

        # connect odometry to dummy node
        edges[edge_counter] = graph.Edge(
            startuid=current_odom_vertex_uid,
            enduid=dummy_node_uid,
            information=np.eye(6),
            measurement=np.array([0, 0, 0, 0, 0, 0, 1])
        )
        edge_counter += 1

        previous_vertex = current_odom_vertex_uid
        previous_pose_matrix = pose_matrices[i]

    resulting_graph = graph.Graph(vertices, edges, gravity_axis='y', damping_status=True)
    return resulting_graph
