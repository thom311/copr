from __future__ import absolute_import

import os
import requests
from munch import Munch
from requests_toolbelt.multipart.encoder import MultipartEncoder, MultipartEncoderMonitor
from .helpers import List
from .exceptions import CoprRequestException


GET = "GET"
POST = "POST"


class Request(object):
    # This should be a replacement of the _fetch method from APIv1
    # We can have Request, FileRequest, AuthRequest/UnAuthRequest, ...

    def __init__(self, endpoint, api_base_url=None, method=None, data=None, params=None, auth=None):
        """
        :param endpoint:
        :param api_base_url:
        :param method:
        :param data: dict
        :param params: dict for constructing query params in URL (e.g. ?key1=val1)
        :param auth: tuple (login, token)

        @TODO maybe don't have both params and data, but rather only one variable
        @TODO and send it as data on POST and as params on GET
        """
        self.endpoint = endpoint
        self.api_base_url = api_base_url
        self._method = method or GET
        self.data = data
        self.params = params
        self.auth = auth
        self.headers = None

    @property
    def endpoint_url(self):
        return os.path.join(self.api_base_url, self.endpoint.strip("/"))

    @property
    def method(self):
        return self._method.upper()

    def send(self):
        response = requests.request(method=self.method, url=self.endpoint_url, data=self.data,
                                    params=self.params, auth=self.auth, headers=self.headers)
        handle_errors(response.json())
        return Response(headers=response.headers, data=response.json(), request=self)


class FileRequest(Request):
    def __init__(self, *args, **kwargs):
        super(FileRequest, self).__init__(*args, **kwargs)
        callback = lambda x: x  # @TODO progress_callback or (lambda x: x)
        m = MultipartEncoder(self.data)
        self.data = MultipartEncoderMonitor(m, callback)
        self.headers = {'Content-Type': self.data.content_type}


class Response(object):
    def __init__(self, headers=None, data=None, request=None):
        self.headers = headers or {}
        self.data = data or {}
        self.request = request

    def munchify(self):
        if "items" in self.data:
            # @TODO add test case for being a list
            return List(items=[Munch(obj) for obj in self.data["items"]],
                        meta=Munch(self.data["meta"]), response=self)
        return Munch(self.data, __response__=self)


def handle_errors(response_json):
    if "error" in response_json:
        raise CoprRequestException(response_json["error"])