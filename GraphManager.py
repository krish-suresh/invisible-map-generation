#!/usr/bin/env python3
"""
Contains the GraphManager class and a main routine that makes use of it.

Optional arguments if running this script as main:
  -h, --help  show this help message and exit
  -p P        Pattern to match to graph names; matching graph names in cache are optimized and plotted (e.g., '-g
              *Living_Room*' will plot any cached map with 'Living_Room' in its name); if no pattern is specified,
              then all cached maps are plotted and optimized (default pattern is '*'). The cache directory is
              searched recursively, and '**/' is automatically prepended to the pattern
  -f          Acquire maps from firebase and overwrite existing cache. Mutually exclusive with the rest of the options.
  -F          Upload optimized any graphs to firebase that are optimized while this script is running.

Notes:
- This script was adapted from the script test_firebase_sba as of commit 74891577511869f7cd3c4743c1e69fb5145f81e0

Author: Duncan Mazza
"""

import argparse
import glob
import json
import os

import firebase_admin
import matplotlib.pyplot as plt
import numpy as np
from firebase_admin import credentials
from firebase_admin import db
from firebase_admin import storage
from g2o import SE3Quat, Quaternion
from varname import nameof

import convert_json_sba
import graph_utils


class GraphManager:
    """Class that manages graphs by interfacing with firebase, keeping a cache of data downloaded from firebase, and
    providing methods wrapping graph optimization and plotting capabilities.

    Class Attributes:
        _weights_dict (Dict[str, np.ndarray]): Maps descriptive names of weight vectors to the corresponding weight
         vector, Higher values in the vector indicate greater noise (note: the uncertainty estimates of translation 
         seem to be pretty over optimistic, hence the large correction here) for the orientation
        _app_initialize_dict (Dict[str, str]): Used for initializing the `app` attribute
        _unprocessed_maps_bucket_ref (str): Specifies Firebase bucket reference for unprocessed maps
        _processed_maps_bucket_ref

    Attributes:
        _app (firebase_admin.App): App initialized with a service account, granting admin privileges
        _bucket: Handle to a Google Cloud Storage bucket
        _unprocessed_map_ref: Database reference representing the node for the unprocessed maps
        _selected_weights (np.ndarray): Vector selected from the `GraphManager._weights_dict`
        _cache_path (str): String representing the absolute path to the cache folder. The cache path is evaluated to
         always be located at `<path to this file>.cache/`
    """
    _weights_dict = {
        "sensible_default_weights": np.array([
            -6., -6., -6., -6., -6., -6.,
            18, 18, 0, 0, 0, 0,
            0., 0., 0., -1, 1e2, -1
        ]),
        "trust_odom": np.array([
            -3., -3., -3., -3., -3., -3.,
            10.6, 10.6, 10.6, 10.6, 10.6, 10.6,
            0., 0., 0., -1, -1, 1e2
        ]),
        "trust_tags": np.array([
            10, 10, 10, 10, 10, 10,
            -10.6, -10.6, -10.6, -10.6, -10.6, -10.6,
            0, 0, 0, -1e2, 3, 3
        ]),
    }
    _app_initialize_dict = {
        'databaseURL': 'https://invisible-map-sandbox.firebaseio.com/',
        'storageBucket': 'invisible-map.appspot.com'
    }
    _unprocessed_maps_bucket_ref = "unprocessed_maps"
    _processed_maps_bucket_ref = "TestProcessed"

    def __init__(self, weights_specifier, cred):
        """Initializes GraphManager instance (only populates instance attributes)

        Args:
             weights_specifier: Used as the key to access the corresponding value in `GraphManager._weights_dict`
             cred: Firebase credentials to pass as the first argument to `firebase_admin.initialize_app(cred, ...)`
        """
        self._app = firebase_admin.initialize_app(cred, GraphManager._app_initialize_dict)
        self._bucket = storage.bucket(app=self._app)
        self._unprocessed_map_ref = db.reference("/" + GraphManager._unprocessed_maps_bucket_ref)
        self._selected_weights = str(weights_specifier)
        self._cache_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), ".cache")

    def firebase_listen_unprocessed_maps(self):
        """Invokes the `listen` method of the `_unprocessed_map_ref` attribute and provides the
        `_unprocessed_maps_callback` method as the callback function argument.
        """
        self._unprocessed_map_ref.listen(self._unprocessed_maps_callback)

    def process_maps(self, pattern, visualize=True, upload=False):
        """Invokes optimization and plotting routines for any cached graphs matching the specified pattern.

        The `_resolve_cache_dir` method is first called, then the `glob` package is used to find matching files.
        Matching maps' json strings are loaded, parsed, and provided to the `_process_map` method. If an exception is
        raised in the process of loading a map or processing it, it is caught and its details are printed to the
        command line.

        Args:
            pattern (str): Pattern to find matching cached graphs (which are stored as `.json` files. The cache
             directory (specified by the `_cache_path` attribute) is searched recursively, and '**/' is automatically
             prepended to the pattern.
            visualize (bool): Value passed as the visualize argument to the invocation of the `_process_map` method.
            upload (bool): Value passed as the upload argument to the invocation of the `_process_map` method.
        """
        self._resolve_cache_dir()
        matching_maps = glob.glob(os.path.join(self._cache_path, "**/" + pattern), recursive=True)

        if len(matching_maps) == 0:
            print("No maps matching pattern {} in recursive search of {}".format(pattern, self._cache_path))
            return

        for map_json_abs_path in matching_maps:
            print("Attempting to process map {}".format(map_json_abs_path))
            try:
                with open(os.path.join(self._cache_path, map_json_abs_path), "r") as json_string_file:
                    json_string = json_string_file.read()
                    json_string_file.close()
                map_json = os.path.sep.join(map_json_abs_path.split(os.path.sep)[len(self._cache_path.split(
                    os.path.sep)) + 1:])
                map_dct = json.loads(json_string)
                map_name = self._read_cache_directory(os.path.basename(map_json))
                self._process_map(map_name, map_json, map_dct, visualize, upload)
            except Exception as ex:
                print("Could not process cached map at {} due to error: {}".format(map_json_abs_path, ex))

    # -- Private Methods --

    def _firebase_get_unprocessed_map(self, map_name, map_json):
        """Acquires a map from the specified blob and caches it.

        A diagnostic message is printed if the `map_json` blob name was not found by Firebase.

        Args:
            map_name (str): Value passed as the `map_name` argument to the `_cache_map` method; the data in map_name is
             ultimately used for uploading a map to firebase by specifying the child of the 'maps' database reference.
            map_json (str): Value passed as the `blob_name` argument to the `get_blob` method of the `_bucket`
             attribute.

        Returns:
            True if the map was successfully acquired and cached, and false if the map was not found by Firebase
        """
        json_blob = self._bucket.get_blob(map_json)
        if json_blob is not None:
            json_data = json_blob.download_as_bytes()
            json_string = json.loads(json_data)
            self._cache_map(GraphManager._unprocessed_maps_bucket_ref, map_name, map_json, json.dumps(json_string))
            return True
        else:
            print("Map '{}' was missing".format(map_name))
            return False

    def _process_map(self, map_name, map_json, map_dct, visualize=False, upload=False):
        tag_locations, odom_locations, waypoint_locations = self._optimize_map(map_dct, False, visualize)
        processed_map_json = GraphManager.make_processed_map_JSON(tag_locations, odom_locations, waypoint_locations)
        self._cache_map(GraphManager._processed_maps_bucket_ref, map_name, map_json, processed_map_json)

        if upload:
            processed_map_filename = os.path.basename(map_json)[:-5] + '_processed.json'
            processed_map_full_path = os.path.join(GraphManager._processed_maps_bucket_ref, processed_map_filename)
            processed_map_blob = self._bucket.blob(processed_map_full_path)
            processed_map_blob.upload_from_string(processed_map_json)
            db.reference('maps').child(map_name).child('map_file').set(processed_map_full_path)
        print("processed map", map_name)

    def _append_to_cache_directory(self, key, value):
        """Appends the specified key-value pair to the dictionary stored as a json file in
        `<cache folder>/directory.json`.

        If the key already exists in the dictionary, its value is overwritten. Note that no error handling is
        implemented.

        Args:
            key (str): Key to store `value` in
            value (str): Value to store under `key`
        """
        directory_json_path = os.path.join(self._cache_path, "directory.json")
        with open(directory_json_path, "r") as directory_file_read:
            directory_json = json.loads(directory_file_read.read())
            directory_file_read.close()
        directory_json[key] = value
        new_directory_json = json.dumps(directory_json)
        with open(directory_json_path, "w") as directory_file_write:
            directory_file_write.write(new_directory_json)
            directory_file_write.close()

    def _read_cache_directory(self, key):
        """Reads the dictionary stored as a json file in `<cache folder>/directory.json` and returns the value
        associated with the specified key.

        Note that no error handling is implemented.

        Args:
            key (str): Key to query the dictionary

        Returns:
            Value associated with the key
        """
        with open(os.path.join(self._cache_path, "directory.json"), "r") as directory_file:
            directory_json = json.loads(directory_file.read())
            directory_file.close()
            return directory_json[key]

    def _cache_map(self, bucket_ref, map_name, map_json, json_string):
        """Saves a map to a json file in cache directory.

        Catches any exceptions raised and displays an appropriate diagnostic message if one is caught. All of the
        arguments are checked to ensure that they are, in fact strings; if any are not, then a diagnostic message is
        printed and False is returned.

        Arguments:
            bucket_ref (str): String specifying the bucket reference under which the map is stored; should equal one
             of the class attributes `GraphManager._unprocessed_maps_bucket_ref` or
             `GraphManager._processed_maps_bucket_ref`
            map_name (str): String specifying the appropriate child node of the 'maps' node in the database; the map
             name is mapped to the value of `map_json` in the `<cache folder>/directory.json` dictionary for later
             reference when loading the map from cache
            map_json (str): String corresponding to both the bucket blob name of the map and the path to cache the
             map relative to `bucket_ref`
            json_string (str): The json string that defines the map (this is what is written as the contents of the
             cached map file)

        Returns:
            True if map was successfully cached, and False otherwise
        """
        for arg in [bucket_ref, map_name, map_json, json_string]:
            if not isinstance(arg, str):
                print("Cannot cache map because '{}' argument is not a string".format(nameof(arg)))
                return False
        if not self._resolve_cache_dir():
            print("Cannot cache map because cache folder existence could not be resolved at path {}".format(
                self._cache_path))
            return False

        cached_file_path = os.path.join(self._cache_path, bucket_ref, map_json)
        try:
            map_json_split = map_json.split("/")
            map_json_split_idx = 0
            while map_json_split_idx < len(map_json_split) - 1:
                dir_to_check = os.path.join(self._cache_path, bucket_ref, os.path.sep.join(map_json_split[
                                                                                    :map_json_split_idx + 1]))
                if not os.path.exists(dir_to_check):
                    os.mkdir(dir_to_check)
                map_json_split_idx += 1

            with open(cached_file_path, "w") as map_json_file:
                map_json_file.write(json_string)
                map_json_file.close()

            self._append_to_cache_directory(os.path.basename(map_json), map_name)
            print("Successfully cached {}".format(cached_file_path))
            return True
        except Exception as ex:
            print("Could not cache map {} due to error: {}".format(map_json, ex))
            return False

    def _resolve_cache_dir(self):
        """Returns true if the cache folder exists, and attempts to create a new one if there is none.

        The cache folder is specified by the absolute path in the `_cache_dir` attribute. Two subdirectories named
        after the relevant bucket paths (as specified by `GraphManager._processed_maps_bucket_ref` and
        `GraphManager._unprocessed_maps_bucket_ref` are also created. A file named `directory.json` is also created in
        the cache folder.

        This method catches all exceptions associated with creating new directories/files and displays a corresponding
        diagnostic message.

        Returns:
            True if no exceptions were caught and False otherwise
        """
        processed_path = os.path.join(self._cache_path, GraphManager._processed_maps_bucket_ref)
        unprocessed_path = os.path.join(self._cache_path, GraphManager._unprocessed_maps_bucket_ref)
        for path in [self._cache_path, processed_path, unprocessed_path]:
            if os.path.exists(path):
                continue
            try:
                os.mkdir(path)
            except Exception as ex:
                print("Could not create a cache directory at {} due to error: {}".format(path, ex))
                return False

        directory_path = os.path.join(self._cache_path, "directory.json")
        if not os.path.exists(directory_path):
            try:
                with open(os.path.join(self._cache_path, "directory.json"), "w") as directory_file:
                    directory_file.write(json.dumps({}))
                    directory_file.close()
                return True
            except Exception as ex:
                print("Could not create {} file due to error: {}".format(directory_path, ex))
        else:
            return True

    def _unprocessed_maps_callback(self, m):
        """Callback function used in the `firebase_listen_unprocessed_maps` method.
        """
        if type(m.data) == str:
            # A single new map just got added
            self._firebase_get_unprocessed_map(m.path.lstrip('/'), m.data)
        elif type(m.data) == dict:
            # This will be a dictionary of all the data that is there initially
            for map_name, map_json in m.data.items():
                self._firebase_get_unprocessed_map(map_name, map_json)

    def _optimize_map(self, dct, tune_weights=False, visualize=False):
        """Map optimization routine.

        TODO: more detailed documentation
        """
        test_graph = convert_json_sba.as_graph(dct)
        test_graph.weights = GraphManager._weights_dict[self._selected_weights]

        # Load these weights into the graph
        test_graph.update_edges()
        test_graph.generate_unoptimized_graph()

        # Commented out: unused
        # all_tags_original = graph_utils.get_tags_all_position_estimate(test_graph)

        starting_map = graph_utils.optimizer_to_map(
            test_graph.vertices, test_graph.unoptimized_graph, is_sparse_bundle_adjustment=True)
        original_tag_verts = GraphManager.locations_from_transforms(starting_map['tags'])
        if tune_weights:
            test_graph.expetation_maximization_once()
            print("tuned weights", test_graph.weights)

        # Create the g2o object and optimize
        test_graph.generate_unoptimized_graph()
        test_graph.optimize_graph()

        # Change vertex estimates based off the optimized graph
        test_graph.update_vertices()

        prior_map = graph_utils.optimizer_to_map(
            test_graph.vertices, test_graph.unoptimized_graph)
        resulting_map = graph_utils.optimizer_to_map(
            test_graph.vertices,
            test_graph.optimized_graph,
            is_sparse_bundle_adjustment=True)
        prior_locations = GraphManager.locations_from_transforms(prior_map['locations'])
        locations = GraphManager.locations_from_transforms(resulting_map['locations'])

        tag_verts = GraphManager.locations_from_transforms(resulting_map['tags'])
        tagpoint_positions = resulting_map['tagpoints']
        waypoint_verts = resulting_map['waypoints']
        if visualize:
            f = plt.figure()
            ax = f.add_subplot(111, projection='3d')
            plt.plot(locations[:, 0], locations[:, 1], locations[:, 2], '.', c='b', label='Odom Vertices')
            plt.plot(prior_locations[:, 0], prior_locations[:, 1], prior_locations[:, 2], '.', c='g',
                     label='Prior Odom Vertices')
            plt.plot(original_tag_verts[:, 0], original_tag_verts[:, 1], original_tag_verts[:, 2], 'o', c='c',
                     label='Tag Vertices Original')
            plt.plot(tag_verts[:, 0], tag_verts[:, 1], tag_verts[:, 2], 'o', c='r', label='Tag Vertices')
            for tag_vert in tag_verts:
                R = Quaternion(tag_vert[3:-1]).rotation_matrix()
                axis_to_color = ['r', 'g', 'b']
                for axis_id in range(3):
                    ax.quiver(tag_vert[0], tag_vert[1], tag_vert[2], R[0, axis_id], R[1, axis_id],
                              R[2, axis_id], length=1, color=axis_to_color[axis_id])
            plt.plot(tagpoint_positions[:, 0], tagpoint_positions[:, 1], tagpoint_positions[:, 2], '.', c='m',
                     label='Tag Corners')
            for vert in tag_verts:
                ax.text(vert[0], vert[1], vert[2], str(int(vert[-1])), color='black')
            plt.plot(waypoint_verts[1][:, 0], waypoint_verts[1][:, 1], waypoint_verts[1][:, 2], 'o', c='y',
                     label='Waypoint Vertices')
            for vert_idx in range(len(waypoint_verts[0])):
                vert = waypoint_verts[1][vert_idx]
                waypoint_name = waypoint_verts[0][vert_idx]['name']
                ax.text(vert[0], vert[1], vert[2], waypoint_name, color='black')

            # plt.plot(all_tags[:, 0], all_tags[:, 1], all_tags[:, 2], '.', c='g', label='All Tag Edges')
            # plt.plot(all_tags_original[:, 0], all_tags_original[:, 1], all_tags_original[:, 2], '.', c='m',
            #          label='All Tag Edges Original')

            # Commented-out: unused
            # all_tags = graph_utils.get_tags_all_position_estimate(test_graph)
            # tag_edge_std_dev_before_and_after = compare_std_dev(all_tags, all_tags_original)

            tag_vertex_shift = original_tag_verts - tag_verts
            print("tag_vertex_shift", tag_vertex_shift)
            plt.legend()
            GraphManager.axis_equal(ax)
            plt.show()
        return tag_verts, locations, waypoint_verts

    # -- Static Methods --

    @staticmethod
    def locations_from_transforms(locations):
        for i in range(locations.shape[0]):
            locations[i, :7] = SE3Quat(locations[i, :7]).inverse().to_vector()
        return locations

    @staticmethod
    def axis_equal(ax):
        """Create cubic bounding box to simulate equal aspect ratio
        """
        axis_range_from_limits = lambda limits: limits[1] - limits[0]
        max_range = np.array([axis_range_from_limits(ax.get_xlim()), axis_range_from_limits(ax.get_ylim()),
                              axis_range_from_limits(ax.get_zlim())]).max()
        Xb = 0.5 * max_range * np.mgrid[-1:2:2, -1:2:2, -1:2:2][0].flatten() + 0.5 * (
                ax.get_xlim()[1] + ax.get_xlim()[0])
        Yb = 0.5 * max_range * np.mgrid[-1:2:2, -1:2:2, -1:2:2][1].flatten() + 0.5 * (
                ax.get_ylim()[1] + ax.get_ylim()[0])
        Zb = 0.5 * max_range * np.mgrid[-1:2:2, -1:2:2, -1:2:2][2].flatten() + 0.5 * (
                ax.get_zlim()[1] + ax.get_zlim()[0])

        # Comment or uncomment following both lines to test the fake bounding box:
        for xb, yb, zb in zip(Xb, Yb, Zb):
            ax.plot([xb], [yb], [zb], 'w')

    @staticmethod
    def compare_std_dev(all_tags, all_tags_original):
        return {int(tag_id): (np.std(all_tags_original[all_tags_original[:, -1] == tag_id, :-1], axis=0),
                              np.std(all_tags[all_tags[:, -1] == tag_id, :-1], axis=0)) for tag_id in
                np.unique(all_tags[:, -1])}

    @staticmethod
    def make_processed_map_JSON(tag_locations, odom_locations, waypoint_locations):
        tag_vertex_map = map(lambda curr_tag: {
            'translation': {'x': curr_tag[0], 'y': curr_tag[1], 'z': curr_tag[2]},
            'rotation': {'x': curr_tag[3],
                         'y': curr_tag[4],
                         'z': curr_tag[5],
                         'w': curr_tag[6]},
            'id': int(curr_tag[7])}, tag_locations)
        odom_vertex_map = map(lambda curr_odom: {
            'translation': {'x': curr_odom[0], 'y': curr_odom[1],
                            'z': curr_odom[2]},
            'rotation': {'x': curr_odom[3],
                         'y': curr_odom[4],
                         'z': curr_odom[5],
                         'w': curr_odom[6]},
            'poseId': int(curr_odom[8])}, odom_locations)
        waypoint_vertex_map = map(lambda idx: {
            'translation': {'x': waypoint_locations[1][idx][0],
                            'y': waypoint_locations[1][idx][1],
                            'z': waypoint_locations[1][idx][2]},
            'rotation': {'x': waypoint_locations[1][idx][3],
                         'y': waypoint_locations[1][idx][4],
                         'z': waypoint_locations[1][idx][5],
                         'w': waypoint_locations[1][idx][6]},
            'id': waypoint_locations[0][idx]['name']},
                                  range(len(waypoint_locations[0])))
        return json.dumps({'tag_vertices': list(tag_vertex_map),
                           'odometry_vertices': list(odom_vertex_map),
                           'waypoints_vertices': list(waypoint_vertex_map)})


def make_parser():
    """Makes an argument parser object for this program

    Returns:
        Argument parser
    """
    parser = argparse.ArgumentParser(description="Acquire (from cache or Firebase) graphs, run optimization, and plot")
    parser.add_argument(
        "-p",
        type=str,
        help="Pattern to match to graph names; matching graph names in cache are optimized and plotted (e.g., "
             "'-g *Living_Room*' will plot any cached map with 'Living_Room' in its name); if no pattern is specified, "
             "then all cached maps are plotted and optimized (default pattern is '*'). The cache directory is searched "
             "recursively, and '**/' is automatically prepended to the pattern"
    )
    parser.add_argument(
        "-f",
        action="store_true",
        help="Acquire maps from firebase and overwrite existing cache. Mutually exclusive with the rest of the options."
    )
    parser.add_argument(
        "-F",
        action="store_true",
        help="Upload optimized any graphs to firebase that are optimized while this script is running."
    )
    return parser


if __name__ == "__main__":
    parser = make_parser()
    args = parser.parse_args()

    if args.f and (args.p or args.F):
        print("Option in addition to -f specified, but -f optoin is mutually exclusive with other options due to the "
              "asynchronous nature of Firebase updating.")
        exit()

    # Fetch the service account key JSON file contents
    cred = credentials.Certificate(os.environ.get('GOOGLE_APPLICATION_CREDENTIALS'))
    graph_handler = GraphManager("sensible_default_weights", cred)

    if args.f:
        graph_handler.firebase_listen_unprocessed_maps()
    else:
        if args.p:
            map_pattern = args.p
        else:
            map_pattern = "*"

        graph_handler.process_maps(map_pattern, upload=args.F)
