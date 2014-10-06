# Copyright 2014 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You
# may not use this file except in compliance with the License. A copy of
# the License is located at
#
# http://aws.amazon.com/apache2.0/
#
# or in the "license" file accompanying this file. This file is
# distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF
# ANY KIND, either express or implied. See the License for the specific
# language governing permissions and limitations under the License.

import jmespath
from botocore import xform_name


def all_not_none(iterable):
    """
    Return True if all elements of the iterable are not None (or if the
    iterable is empty). This is like the built-in ``all``, except checks
    against None, so 0 and False are allowable values.
    """
    for element in iterable:
        if element is None:
            return False
    return True


def build_identifiers(identifiers_def, parent, params, raw_response):
    """
    Builds a mapping of identifier names to values based on the
    identifier source location, type, and target. Identifier
    values may be scalars or lists depending on the source type
    and location.

    :type identifiers_def: list
    :param identifiers_def: List of identifier definitions
    :type parent: ServiceResource
    :param parent: The resource instance to which this action is attached.
    :type params: dict
    :param params: Request parameters sent to the service.
    :type response: dict
    :param response: Low-level operation response.
    """
    results = {}

    for identifier in identifiers_def:
        source = identifier.get('source', '')
        source_type = identifier.get('sourceType')
        target = identifier.get('target')

        if source_type == 'responsePath':
            value = jmespath.search(source, raw_response)
            results[xform_name(target)] = value
        elif source_type in ['identifier', 'dataMember']:
            value = getattr(parent, xform_name(source))
            results[xform_name(target)] = value
        elif source_type == 'requestParameter':
            value = params[source]
            results[xform_name(target)] = value
        else:
            raise NotImplementedError(
                'Unsupported source type: {0}'.format(source_type))

    return results


def build_empty_response(search_path, operation_name, service_model):
    """
    Creates an appropriate empty response for the type that is expected,
    based on the service model's shape type. For example, a value that
    is normally a list would then return an empty list. A structure would
    return an empty dict, and a number would return None.

    :type search_path: string
    :param search_path: JMESPath expression to search in the response
    :type operation_name: string
    :param operation_name: Name of the underlying service operation
    :type service_model: :ref:`botocore.model.ServiceModel`
    :param service_model: The Botocore service model
    :rtype: dict, list, or None
    :return: An appropriate empty value
    """
    response = None

    operation_model = service_model.operation_model(operation_name)
    shape = operation_model.output_shape

    if search_path:
        # Walk the search path and find the final shape. For example, given
        # a path of ``foo.bar[0].baz``, we first find the shape for ``foo``,
        # then the shape for ``bar`` (ignoring the indexing), and finally
        # the shape for ``baz``.
        for item in search_path.split('.'):
            item = item.strip('[0123456789]$')

            if shape.type_name == 'structure':
                shape = shape.members[item]
            elif shape.type_name == 'list':
                shape = shape.member
            else:
                raise NotImplementedError(
                    'Search path hits shape type {0} from {1}'.format(
                        shape.type_name, item))

    # Anything not handled here is set to None
    if shape.type_name == 'structure':
        response = {}
    elif shape.type_name == 'list':
        response = []
    elif shape.type_name == 'map':
        response = {}

    return response


class RawHandler(object):
    """
    A raw action response handler. This passed through the response
    dictionary, optionally after performing a JMESPath search if one
    has been defined for the action.

    :type search_path: string
    :param search_path: JMESPath expression to search in the response
    :rtype: dict
    :return: Service response
    """
    def __init__(self, search_path):
        self.search_path = search_path

    def __call__(self, parent, params, response):
        """
        :type parent: ServiceResource
        :param parent: The resource instance to which this action is attached.
        :type params: dict
        :param params: Request parameters sent to the service.
        :type response: dict
        :param response: Low-level operation response.
        """
        # TODO: Remove the '$' check after JMESPath supports it
        if self.search_path and self.search_path != '$':
            response = jmespath.search(self.search_path, response)

        return response


class ResourceHandler(object):
    """
    Creates a new resource or list of new resources from the low-level
    response based on the given response resource definition.

    :type search_path: string
    :param search_path: JMESPath expression to search in the response
    :type factory: ResourceFactory
    :param factory: The factory that created the resource class to which
                    this action is attached.
    :type resource_defs: dict
    :param resource_defs: Service resource definitions.
    :type service_model: :ref:`botocore.model.ServiceModel`
    :param service_model: The Botocore service model
    :type resource_def: dict
    :param resource_def: Response resource definition.
    :type operation_name: string
    :param operation_name: Name of the underlying service operation
    :rtype: ServiceResource or list
    :return: New resource instance(s).
    """
    def __init__(self, search_path, factory, resource_defs, service_model,
                 resource_def, operation_name):
        self.search_path = search_path
        self.factory = factory
        self.resource_defs = resource_defs
        self.service_model = service_model
        self.resource_def = resource_def
        self.operation_name = operation_name

    def __call__(self, parent, params, response):
        """
        :type parent: ServiceResource
        :param parent: The resource instance to which this action is attached.
        :type params: dict
        :param params: Request parameters sent to the service.
        :type response: dict
        :param response: Low-level operation response.
        """
        resource_name = self.resource_def.get('type', '')
        resource_cls = self.factory.load_from_definition(
            parent.meta['service_name'], resource_name,
            self.resource_defs.get(resource_name), self.resource_defs,
            self.service_model)

        raw_response = response
        search_response = None

        # Anytime a path is defined, it means the response contains the
        # resource's attributes, so resource_data gets set here. It
        # eventually ends up in resource.meta['data'], which is where
        # the attribute properties look for data.
        if self.search_path:
            search_response = jmespath.search(self.search_path, raw_response)

        # First, we parse all the identifiers, then create the individual
        # response resources using them. Any identifiers that are lists
        # will have one item consumed from the front of the list for each
        # resource that is instantiated. Items which are not a list will
        # be set as the same value on each new resource instance.
        identifiers = build_identifiers(
            self.resource_def.get('identifiers', []), parent, params,
            raw_response)

        # If any of the identifiers is a list, then the response is plural
        plural = [v for v in identifiers.values() if isinstance(v, list)]

        if plural:
            response = []

            # The number of items in an identifier that is a list will
            # determine how many resource instances to create.
            for i in range(len(plural[0])):
                # Response item data is *only* available if a search path
                # was given. This prevents accidentally loading unrelated
                # data that may be in the response.
                response_item = None
                if search_response:
                    response_item = search_response[i]
                response.append(self.handle_response_item(resource_cls,
                    parent, identifiers, response_item))
        elif all_not_none(identifiers.values()):
            # All identifiers must always exist, otherwise the resource
            # cannot be instantiated.
            response = self.handle_response_item(resource_cls,
                parent, identifiers, search_response)
        else:
            # The response is should be empty, but that may mean an
            # empty dict, list, or None.
            response = build_empty_response(self.search_path,
                self.operation_name, self.service_model)

        return response

    def handle_response_item(self, resource_cls, parent, identifiers,
                             resource_data):
        """
        Handles the creation of a single response item by setting
        parameters and creating the appropriate resource instance.

        :type resource_cls: ServiceResource subclass
        :param resource_cls: The resource class to instantiate.
        :type parent: ServiceResource
        :param parent: The resource instance to which this action is attached.
        :type identifiers: dict
        :param identifiers: Map of identifier names to value or values.
        :type resource_data: dict or None
        :param resource_data: Data for resource attributes.
        :rtype: ServiceResource
        :return: New resource instance.
        """
        kwargs = {
            'client': parent.meta.get('client'),
        }

        for name, value in identifiers.items():
            # If value is a list, then consume the next item
            if isinstance(value, list):
                value = value.pop(0)

            kwargs[name] = value

        resource = resource_cls(**kwargs)

        if resource_data is not None:
            resource.meta['data'] = resource_data

        return resource
