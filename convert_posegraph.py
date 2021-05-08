"""Convert an old formatted posegraph to the new format.
The new format is useful because it keeps the g2o graph as an object
rather than a file generated by the command line command.
"""
import numpy as np
from graph import Vertex, Edge, Graph, VertexType


def convert_vertex(vertex):
    """Convert the old type of vertex to the new type.

    Args:
        vertex: An old type vertex to be converted to the new format.

    Returns:
        A vertex of the new type with the same information as the
        input vertex.
    """
    if vertex.type == 'tag':
        vertextype = VertexType.TAG
    elif vertex.type == 'odometry':
        if vertex.fix_status:
            vertextype = VertexType.DUMMY
        else:
            vertextype = VertexType.ODOMETRY
    elif vertex.type == 'waypoint':
        vertextype = VertexType.WAYPOINT
    else:
        raise Exception("Vertex type {} not recognized".format(vertex.type))

    return (vertex.id, Vertex(mode=vertextype,
                              estimate=np.concatenate
                              ([vertex.translation, (vertex.rotation)]),
                              fixed=vertex.fix_status
                              ))


def convert_edge(edge):
    """Convert the old type edge to an edge of the new type.

    Args:
        edge: An old type edge to be converted to the new format.

    Returns:
        An edge of the new type with the same information as the input
        edge.
    """
    return Edge(startuid=edge.start.id, enduid=edge.end.id,
                information=edge.importance_matrix,
                information_prescaling=None,
                measurement=np.concatenate
                ([edge.translation, edge.rotation]),
                corner_ids=None,
                camera_intrinsics=None)


def convert(posegraph):
    """Convert the old format for a graph to the new one.

    Args:
        posegraph: An old graph to be converted to the new format.

    Returns:
        A graph of the new type with the same information as the old graph.
    """
    vertices = {}
    edges = {}
    edge_uid = 0

    for startid in posegraph.odometry_edges:
        for endid in posegraph.odometry_edges[startid]:
            edge = posegraph.odometry_edges[startid][endid]
            endpoints = [edge.start, edge.end]

            for vertex in endpoints:
                uid, converted = convert_vertex(vertex)
                vertices[uid] = converted

            converted_edge = convert_edge(edge)
            # if not (vertices[converted_edge.startuid].mode == VertexType.DUMMY
            #         or vertices[converted_edge.enduid].mode
            #         == VertexType.DUMMY):
            edges[edge_uid] = convert_edge(edge)
            edge_uid += 1

    for startid in posegraph.odometry_tag_edges:
        for endid in posegraph.odometry_tag_edges[startid]:
            edge = posegraph.odometry_tag_edges[startid][endid]
            endpoints = [edge.start, edge.end]

            for vertex in endpoints:
                uid, converted = convert_vertex(vertex)
                vertices[uid] = converted

            converted_edge = convert_edge(edge)
            if not (vertices[converted_edge.startuid].mode == VertexType.DUMMY
                    or vertices[converted_edge.enduid].mode
                    == VertexType.DUMMY):
                edges[edge_uid] = convert_edge(edge)
                edge_uid += 1

    for startid in posegraph.odometry_waypoints_edges:
        for endid in posegraph.odometry_waypoints_edges[startid]:
            edge = posegraph.odometry_waypoints_edges[startid][endid]
            endpoints = [edge.start, edge.end]

            for vertex in endpoints:
                uid, converted = convert_vertex(vertex)
                vertices[uid] = converted

            converted_edge = convert_edge(edge)
            if not (vertices[converted_edge.startuid].mode == VertexType.DUMMY
                    or vertices[converted_edge.enduid].mode
                    == VertexType.DUMMY):
                edges[edge_uid] = convert_edge(edge)
                edge_uid += 1

    return Graph(vertices=vertices, edges=edges)
