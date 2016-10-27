import json
import time
from sqlalchemy import or_
from sqlalchemy import and_, bindparam, Integer
from sqlalchemy.sql import false, true, text

from coprs import app
from coprs import db
from coprs import exceptions
from coprs import models
from coprs import helpers
from coprs import forms

from coprs.logic import coprs_logic
from coprs.logic import users_logic
from coprs.logic import builds_logic

from coprs.constants import DEFAULT_BUILD_TIMEOUT

log = app.logger


class PackagesLogic(object):

    @classmethod
    def get_by_id(cls, package_id):
        return models.Package.query.filter(models.Package.id == package_id)

    @classmethod
    def get_all(cls, copr_id):
        return (models.Package.query
                .filter(models.Package.copr_id == copr_id))

    @classmethod
    def get_copr_packages_list(cls, copr):
        query_select = """
SELECT package.name, build.pkg_version, build.submitted_on, package.webhook_rebuild, order_to_status(subquery2.min_order_for_a_build) AS status
FROM package
LEFT OUTER JOIN (select MAX(build.id) as max_build_id_for_a_package, package_id
  FROM build
  WHERE build.copr_id = :copr_id
  GROUP BY package_id) as subquery1 ON subquery1.package_id = package.id
LEFT OUTER JOIN build ON build.id = subquery1.max_build_id_for_a_package
LEFT OUTER JOIN (select build_id, min(status_to_order(status)) as min_order_for_a_build
  FROM build_chroot
  GROUP BY build_id) as subquery2 ON subquery2.build_id = subquery1.max_build_id_for_a_package
WHERE package.copr_id = :copr_id;
        """

        if db.engine.url.drivername == "sqlite":
            def sqlite_status_to_order(x):
                if x == 0:
                    return 0
                elif x == 3:
                    return 1
                elif x == 6:
                    return 2
                elif x == 7:
                    return 3
                elif x == 4:
                    return 4
                elif x == 1:
                    return 5
                elif x == 5:
                    return 6
                return 1000

            def sqlite_order_to_status(x):
                if x == 0:
                    return 0
                elif x == 1:
                    return 3
                elif x == 2:
                    return 6
                elif x == 3:
                    return 7
                elif x == 4:
                    return 4
                elif x == 5:
                    return 1
                elif x == 6:
                    return 5
                return 1000

            conn = db.engine.connect()
            conn.connection.create_function("status_to_order", 1, sqlite_status_to_order)
            conn.connection.create_function("order_to_status", 1, sqlite_order_to_status)
            statement = text(query_select)
            statement.bindparams(bindparam("copr_id", Integer))
            result = conn.execute(statement, {"copr_id": copr.id})
        else:
            statement = text(query_select)
            statement.bindparams(bindparam("copr_id", Integer))
            result = db.engine.execute(statement, {"copr_id": copr.id})

        return result

    @classmethod
    def get(cls, copr_id, package_name):
        return models.Package.query.filter(models.Package.copr_id == copr_id,
                                           models.Package.name == package_name)

    @classmethod
    def get_for_webhook_rebuild(cls, copr_id, webhook_secret, clone_url, payload):
        packages = (models.Package.query.join(models.Copr)
                    .filter(models.Copr.webhook_secret == webhook_secret)
                    .filter(models.Package.copr_id == copr_id)
                    .filter(models.Package.webhook_rebuild == true())
                    .filter(models.Package.source_json.contains(clone_url)))

        result = []
        for package in packages:
            if cls.commits_belong_to_package(package, payload):
                result += [package]
        return result

    @classmethod
    def commits_belong_to_package(cls, package, payload):
        ref = payload.get("ref", "")

        if package.source_type_text == "git_and_tito":
            branch = package.source_json_dict["git_branch"]
            if branch and not ref.endswith("/"+branch):
                return False
        elif package.source_type_text == "mock_scm":
            branch = package.source_json_dict["scm_branch"]
            if branch and not ref.endswith("/"+branch):
                return False

        commits = payload.get("commits", [])

        if package.source_type_text == "git_and_tito":
            path_match = False
            for commit in commits:
                for file_path in commit['added'] + commit['removed'] + commit['modified']:
                    if cls.path_belong_to_package(package, file_path):
                        path_match = True
                        break
            if not path_match:
                return False

        return True

    @classmethod
    def path_belong_to_package(cls, package, file_path):
        if package.source_type_text == "git_and_tito":
            data = package.source_json_dict
            return file_path.startswith(data["git_dir"] or '')
        else:
            return True

    @classmethod
    def add(cls, user, copr, package_name, source_type=helpers.BuildSourceEnum("unset"), source_json=json.dumps({})):
        users_logic.UsersLogic.raise_if_cant_build_in_copr(
            user, copr,
            "You don't have permissions to build in this copr.")

        if cls.exists(copr.id, package_name).all():
            raise exceptions.DuplicateException(
                "Project {}/{} already has a package '{}'"
                .format(copr.owner_name, copr.name, package_name))

        package = models.Package(
            name=package_name,
            copr_id=copr.id,
            source_type=source_type,
            source_json=source_json
        )

        db.session.add(package)

        return package

    @classmethod
    def exists(cls, copr_id, package_name):
        return (models.Package.query
                .filter(models.Package.copr_id == copr_id)
                .filter(models.Package.name == package_name))


    @classmethod
    def delete_package(cls, user, package):
        if not user.can_edit(package.copr):
            raise exceptions.InsufficientRightsException(
                "You are not allowed to delete package `{}`.".format(package.id))

        for build in package.builds:
            builds_logic.BuildsLogic.delete_build(user, build)

        db.session.delete(package)


    @classmethod
    def reset_package(cls, user, package):
        if not user.can_edit(package.copr):
            raise exceptions.InsufficientRightsException(
                "You are not allowed to reset package `{}`.".format(package.id))

        package.source_json = json.dumps({})
        package.source_type = helpers.BuildSourceEnum("unset")

        db.session.add(package)


    @classmethod
    def build_package(cls, user, copr, package, chroot_names=None, **build_options):
        if not package.has_source_type_set or not package.source_json:
            raise NoPackageSourceException('Unset default source for package {package}'.format(package.name))
        return builds_logic.BuildsLogic.create_new(user, copr, package.source_type, package.source_json, chroot_names, **build_options)
