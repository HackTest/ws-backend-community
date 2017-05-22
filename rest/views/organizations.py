# -*- coding: utf-8 -*-
from __future__ import absolute_import

import django_filters
from django.core.exceptions import ObjectDoesNotExist
from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view
from rest_framework.exceptions import PermissionDenied, NotFound, ValidationError

from lib import FilesystemHelper
from lib import S3Helper
from rest.responses import DomainsUploadResponse
from rest.responses import NetworksUploadResponse
from tasknode.tasks import initialize_organization, handle_organization_deletion, process_dns_text_file, \
    send_emails_for_org_user_invite
from rest.models import Organization, Network, OrganizationConfig, WsAuthGroup, WsUser, DomainName, ScanPort, PaymentToken
from rest.serializers import OrganizationSerializer, OrganizationNetworkUploadRangeSerializer, \
    OrganizationDomainNameUploadRangeSerializer, NetworkSerializer, DomainNameSerializer
from wselasticsearch.models import UserUploadModel
from .base import WsListCreateChildAPIView, WsListCreateAPIView, \
    WsRetrieveUpdateDestroyAPIView, WsListChildAPIView, BaseWsAPIView
from rest_framework.response import Response
from lib.parsing import NetworksCsvWrapper, DomainsTextFileWrapper, CidrRangeWrapper
from .exception import OperationNotAllowed
from lib import ConfigManager
from lib.smtp import SmtpEmailHelper
import rest.filters
import rest.serializers

config = ConfigManager.instance()


class OrganizationPermissionsMixin(object):
    """
    This is a mixin class for verifying that the requesting user has permissions to query the
    related organization in various manners.
    """

    def _verify_admin_permissions(self):
        """
        Verify that the requesting User has admin permissions for the queried organization.
        :return: None
        """
        organization = get_object_or_404(Organization, pk=self.kwargs["pk"])
        if not organization.can_user_scan(self.request.user):
            raise PermissionDenied("You do not have permission to scan that organization.")

    def _verify_read_permissions(self):
        """
        Verify that the requesting User has read permissions for the queried organization.
        :return: None
        """
        organization = get_object_or_404(Organization, pk=self.kwargs["pk"])
        if not organization.can_user_read(self.request.user):
            raise PermissionDenied("You do not have permission to read from that organization.")

    def _verify_scan_permissions(self):
        """
        Verify that the requesting User has scan permissions for the queried organization.
        :return: None
        """
        organization = get_object_or_404(Organization, pk=self.kwargs["pk"])
        if not organization.can_user_scan(self.request.user):
            raise PermissionDenied("You do not have permission to scan that organization.")

    def _verify_write_permissions(self):
        """
        Verify that the requesting User has write permissions to the queried organization.
        :return: None
        """
        organization = get_object_or_404(Organization, pk=self.kwargs["pk"])
        if not organization.can_user_write(self.request.user):
            raise PermissionDenied("You do not have permission to modify that organization.")


class OrganizationMixin(OrganizationPermissionsMixin):
    """
    This is a mixin class for API views that query data about organizations.
    """

    serializer_class = OrganizationSerializer

    def _get_su_queryset(self):
        return Organization.objects.order_by("-created").all()

    def _get_user_queryset(self):
        return Organization.objects\
            .filter(auth_groups__users=self.request.user, auth_groups__name="org_read") \
            .order_by("-created")\
            .all()

    def perform_destroy(self, instance):
        if not self.request.user.is_superuser:
            self._verify_write_permissions()
        return super(OrganizationMixin, self).perform_destroy(instance)

    def perform_update(self, serializer):
        if not self.request.user.is_superuser:
            self._verify_write_permissions()
        return super(OrganizationMixin, self).perform_update(serializer)


class BaseOrganizationListChildAPIView(WsListChildAPIView, OrganizationPermissionsMixin):
    """
    This is a base class for all views that intend to query the children of an organization.
    """

    def check_object_permissions(self, request, obj):
        if not self.request.user.is_superuser and not self.parent_object.can_user_read(self.request.user):
            raise PermissionDenied("You do not have permission to read data from that organization.")
        else:
            return super(BaseOrganizationListChildAPIView, self).check_object_permissions(request, obj)

    @property
    def parent_class(self):
        return Organization


class BaseOrganizationListCreateChildAPIView(WsListCreateChildAPIView, OrganizationPermissionsMixin):
    """
    This is a base class for all views that intend to query and create children for an organization.
    """

    def check_object_permissions(self, request, obj):
        if not self.request.user.is_superuser and not self.parent_object.can_user_read(self.request.user):
            raise PermissionDenied("You do not have permission to read data from that organization.")
        else:
            return super(BaseOrganizationListCreateChildAPIView, self).check_object_permissions(request, obj)

    def perform_create(self, serializer):
        if not self.request.user.is_superuser and not self.parent_object.can_user_write(self.request.user):
            raise PermissionDenied("You do not have permission to modify data associated with that organization.")
        else:
            return super(BaseOrganizationListCreateChildAPIView, self).perform_create(serializer)

    @property
    def parent_class(self):
        return Organization


class NetworksByOrganizationView(BaseOrganizationListCreateChildAPIView):
    """
    API endpoint for creating OrganizationNetwork objects.
    """

    serializer_class = NetworkSerializer
    filter_backends = (django_filters.rest_framework.DjangoFilterBackend,)
    filter_class = rest.filters.NetworkFilter
    ordering_fields = ("name", "address", "mask_length")

    def _get_parent_mapping(self):
        return {
            "organization": self.parent_object,
        }

    def perform_create(self, serializer):
        """
        Handle the creation of a Network for the referenced organization. There is some complicated logic
        here as Web Sight creates networks for organizations, and those networks are not visible by
        end users. The logic here is to check to see if one of the networks added by Web Sight matches
        the network added here, and if so to update that network instead of creating a new one.
        :param serializer: The serializer to save the new network from.
        :return: None
        """
        if not self.request.user.is_superuser and not self.parent_object.can_user_write(self.request.user):
            raise PermissionDenied("You do not have permission to modify data associated with that organization.")
        cidr_wrapper = CidrRangeWrapper.from_cidr_range(
            address=serializer.validated_data["address"],
            mask_length=serializer.validated_data["mask_length"],
        )
        try:
            existing_network = self.parent_object.networks.get(
                address=cidr_wrapper.parsed_address,
                mask_length=cidr_wrapper.mask_length,
            )
            existing_network.added_by = "user"
            existing_network.name = serializer.validated_data["name"]
            existing_network.save()
        except ObjectDoesNotExist:
            serializer.save()

    def get_queryset(self):
        return super(NetworksByOrganizationView, self).get_queryset()\
            .filter(added_by="user")\
            .order_by("name")

    @property
    def relationship_key(self):
        return "organization_id"

    @property
    def child_attribute(self):
        return "networks"


class DomainNamesByOrganizationView(BaseOrganizationListCreateChildAPIView):
    """
    API endpoint for creating domain name objects for an organization.
    """

    serializer_class = DomainNameSerializer
    filter_backends = (django_filters.rest_framework.DjangoFilterBackend,)
    filter_class = rest.filters.DomainNameFilter
    ordering_fields = ("name",)

    def _get_parent_mapping(self):
        return {
            "organization": self.parent_object,
        }

    def get_queryset(self):
        return super(DomainNamesByOrganizationView, self).get_queryset().order_by("name")

    @property
    def relationship_key(self):
        return "organization_id"

    @property
    def child_attribute(self):
        return "domain_names"


class OrdersByOrganizationView(BaseOrganizationListCreateChildAPIView):
    """
    API endpoint for creating orders based on the current state of an organization.
    """

    serializer_class = rest.serializers.OrderSerializer
    filter_backends = (django_filters.rest_framework.DjangoFilterBackend,)
    filter_class = rest.filters.OrderFilter

    _payment_token = None

    def _get_parent_mapping(self):
        return {
            "organization": self.parent_object,
        }

    def get_queryset(self):
        return self.parent_object.orders.all()

    def create(self, request, *args, **kwargs):
        if not self.request.user.is_superuser:
            self._verify_scan_permissions()
        if self.parent_object.monitored_networks_count == 0 and self.parent_object.monitored_domains_count == 0:
            raise ValidationError(
                "You must choose at least one network or domain name to monitor on an organization before creating a "
                "new order."
            )
        if not self.request.user.is_enterprise_user:
            token_check = self.payment_token
        return super(OrdersByOrganizationView, self).create(request, *args, **kwargs)

    def perform_create(self, serializer):
        if self.request.user.is_enterprise_user:
            serializer.save(organization=self.parent_object)
        else:
            serializer.save(organization=self.parent_object, payment_token=self.payment_token)

    @property
    def payment_token(self):
        """
        Get the payment token associated with the request.
        :return: the payment token associated with the request.
        """
        if self._payment_token is None:
            if "payment_token" not in self.request.data:
                raise ValidationError("You must provide a payment token ID.")
            try:
                token = PaymentToken.objects.get(pk=self.request.data["payment_token"])
                if token.user != self.request.user:
                    raise ValidationError("That payment method was not found.")
                elif not token.can_be_charged:
                    raise ValidationError("That payment method cannot be charged.")
                self._payment_token = token
            except ObjectDoesNotExist:
                raise ValidationError("No payment token found with that ID.")
            except ValueError:
                raise ValidationError("Invalid ID value for payment token.")
        return self._payment_token

    @property
    def relationship_key(self):
        return "organization_id"

    @property
    def child_attribute(self):
        return "orders"


class OrganizationDetailView(OrganizationMixin, WsRetrieveUpdateDestroyAPIView):
    """
    API endpoint for retrieving or manipulating information about a single organization.
    """

    def perform_destroy(self, instance):
        super(OrganizationDetailView, self).perform_destroy(instance)
        handle_organization_deletion.delay(org_uuid=self.kwargs["pk"])


class OrganizationListView(OrganizationMixin, WsListCreateAPIView):
    """
    API endpoint for listing Organization objects.
    """

    filter_backends = (django_filters.rest_framework.DjangoFilterBackend,)
    filter_class = rest.filters.OrganizationFilter
    ordering_fields = ("name", "created")

    def perform_create(self, serializer):
        new_org = serializer.save()
        new_org.add_admin_user(self.request.user)
        initialize_organization.delay(org_uuid=unicode(new_org.uuid))


@api_view(["GET"])
def organization_permissions(request, pk=None):
    """
    This is a function-based view that returns the permissions associated with the requesting user
    and the given organization.
    :param request: The request that invoked this method.
    :param pk: The primary key of the organization being queried.
    :return: A Django response.
    """
    organization = get_object_or_404(Organization, pk=pk)
    if request.user.is_superuser:
        to_return = {
            "user_uuid": request.user.uuid,
            "user_name": request.user.username,
            "can_write": True,
            "can_read": True,
            "can_scan": True,
            "can_admin": True,
        }
    elif not organization.can_user_read(request.user):
        raise NotFound()
    else:
        to_return = {
            "user_uuid": request.user.uuid,
            "user_name": request.user.username,
            "can_write": organization.can_user_read(request.user),
            "can_read": organization.can_user_write(request.user),
            "can_scan": organization.can_user_scan(request.user),
            "can_admin": organization.can_user_admin(request.user),
        }
    return Response(to_return)


class BaseOrganizationAPIView(BaseWsAPIView):
    """
    This is a base APIView class for all APIView implementations that interact with Organization objects
    that do not follow the standard list/create/update etc.
    """

    # Class Members

    _organization = None

    # Instantiation

    # Static Methods

    # Class Methods

    # Public Methods

    def check_permissions(self, request):
        """
        Check to ensure that the requesting user has sufficient permissions to be performing the
        given query.
        :param request: The request that invoked this method.
        :return: None
        """
        super(BaseOrganizationAPIView, self).check_permissions(request)
        self.__check_object_exists()

    def initial(self, request, *args, **kwargs):
        """
        Handle all initialization necessary for setting up this handler.
        :param request: The request that was received.
        :param args: Positional arguments.
        :param kwargs: Keyword arguments.
        :return: None
        """
        self._organization = None
        return super(BaseOrganizationAPIView, self).initial(request, *args, **kwargs)

    # Protected Methods

    # Private Methods

    def __check_object_exists(self):
        """
        Check to make sure that the requested organization exists.
        :return: None
        """
        test = self.organization

    # Properties

    @property
    def organization(self):
        """
        Get the organization that this handler is currently querying.
        :return: the organization that this handler is currently querying.
        """
        if self._organization is None:
            self._organization = get_object_or_404(Organization, pk=self.kwargs["pk"])
        return self._organization

    # Representation and Comparison


class BaseOrganizationWriteAPIView(BaseOrganizationAPIView):
    """
    This is a base APIView class for all APIView implementations that allow users to write to
    the referenced organization.
    """

    def check_permissions(self, request):
        """
        Check to see if the requesting user has write permissions for the queried organization.
        :param request: The request that invoked this method.
        :return: None
        """
        super(BaseOrganizationWriteAPIView, self).check_permissions(request)
        self.__check_write_privs()

    def __check_write_privs(self):
        """
        Check to see if the requesting user has write permissions for the queried organization.
        :return: None
        """
        if not self.request.user.is_superuser:
            if not self.organization.can_user_write(self.request.user):
                raise PermissionDenied("You do not have sufficient permissions")


class BaseOrganizationAdminAPIView(BaseOrganizationAPIView):
    """
    This is a base APIView class for all APIView implementations that allow users to administer
    the referenced organization.
    """

    def check_permissions(self, request):
        """
        Check to see if the requesting user has administrative permissions for the queried
        organization.
        :param request: The request that invoked this method.
        :return: None
        """
        super(BaseOrganizationAdminAPIView, self).check_permissions(request)
        self.__check_admin_privs()

    def __check_admin_privs(self):
        """
        Check to see if the requesting user has administrative privileges for the queried organization.
        :return: None
        """
        if not self.request.user.is_superuser:
            if not self.organization.can_user_admin(self.request.user):
                raise PermissionDenied("You do not have sufficient permissions")


@api_view(["POST"])
def upload_networks_file(request, pk=None):
    """
    This is a function-based view that handles the uploading of a file containing network data
    for a specific organization.
    :param request: The request that resulted in the invocation of this method.
    :param pk: The primary key of the organization to associate the contents of the uploaded file
    with.
    :return: A Django response.
    """
    organization = get_object_or_404(Organization, pk=pk)
    if not request.user.is_superuser:
        if not organization.can_user_write(request.user):
            raise PermissionDenied("You do not have sufficient permissions to add new data to that organization.")
    if "file" not in request.FILES:
        raise NotFound("No file found in request body.")
    uploaded_file = request.FILES["file"]
    if uploaded_file.name.endswith(".csv"):
        wrapper = NetworksCsvWrapper.from_uploaded_file(uploaded_file)
        new_networks, skipped, blacklisted, errored = wrapper.get_new_networks_for_organization(organization)
        organization.save()
        return NetworksUploadResponse(
            new_networks=new_networks,
            skipped=skipped,
            blacklisted=blacklisted,
            errored=errored,
        )
    else:
        raise ValidationError(
            "File type of %s is not supported."
            % (uploaded_file.name[uploaded_file.name.rfind("."):],)
        )


class DomainsUploadAPIView(BaseOrganizationWriteAPIView):
    """
    This is an APIView class that enables users to upload files containing domain names to associate
    with a given organization.
    """

    # Class Members

    # Instantiation

    # Static Methods

    # Class Methods

    # Public Methods

    def post(self, request, pk=None):
        """
        Handle the uploading of a file containing domain names.
        :param request: The request that invoked this method.
        :param pk: The primary key of the Organization to associate the newly-created domain names
        with.
        :return: A Django response.
        """
        if "file" not in request.FILES:
            raise NotFound("No file found in request body.")
        uploaded_file = request.FILES["file"]
        temp_path = FilesystemHelper.get_temporary_file_path()
        FilesystemHelper.write_to_file(file_path=temp_path, data=uploaded_file.read(), write_mode="wb+")
        s3_helper = S3Helper.instance()
        response, key = s3_helper.upload_dns_text_file(
            org_uuid=str(self.organization.uuid),
            local_file_path=temp_path,
            bucket=config.aws_s3_bucket,
        )
        upload_model = UserUploadModel.from_database_model(database_model=self.request.user, upload_type="dns_text")
        upload_model.set_s3_attributes(bucket=config.aws_s3_bucket, key=key, file_type="dns_text")
        upload_model.save(self.organization.uuid)
        contents = FilesystemHelper.get_file_contents(path=temp_path, read_mode="rb")
        if contents.count("\n") > config.rest_domains_file_cutoff:
            process_dns_text_file.delay(
                org_uuid=str(self.organization.uuid),
                file_key=key,
                file_bucket=config.aws_s3_bucket,
            )
            to_return = DomainsUploadResponse(batch_required=True)
        else:
            file_wrapper = DomainsTextFileWrapper(contents)
            new_domains = 0
            skipped_domains = 0
            existing_domains = []
            for entry in self.organization.domain_names.all().values("name"):
                existing_domains.append(entry["name"])
            for row in file_wrapper.rows:
                if row in existing_domains:
                    skipped_domains += 1
                else:
                    new_domains += 1
                    new_domain = DomainName(
                        name=row,
                        is_monitored=False,
                        scanning_enabled=True,
                        organization=self.organization,
                    )
                    new_domain.save()
            to_return = DomainsUploadResponse(
                new_domains=new_domains,
                skipped=skipped_domains,
                errored=len(file_wrapper.errored_rows),
                batch_required=False,
            )
        FilesystemHelper.delete_file(temp_path)
        return to_return

    # Protected Methods

    # Private Methods

    # Properties

    # Representation and Comparison


class OrganizationUserAdminAPIView(BaseOrganizationAdminAPIView):
    """
    This is an APIView that enables users that have administrative privileges for an organization
    to manage the users associated with the organization.
    """

    # Class Members

    # Instantiation

    # Static Methods

    # Class Methods

    # Public Methods

    def patch(self, request, pk=None):
        """
        Handle the removal of a given user from all authorization groups associated with the
        queried organization.
        :param request: The request that invoked this method.
        :param pk: The primary key of the organization being queried.
        :return: A Django response.
        """
        operation = self.get_body_argument("operation")
        self.__validate_patch_operation(operation)
        if operation == "remove_user":
            self.__remove_user()
        elif operation == "add_user":
            self.__add_user()
        elif operation == "update_user":
            self.__update_user()
        return Response(self.__get_user_matrix())

    def get(self, request, pk=None):
        """
        Get information about the users that are currently associated with the queried organization.
        :param request: The request that invoked this method.
        :param pk: The primary key of the organization being queried.
        :return: A Django response.
        """
        return Response(self.__get_user_matrix())

    # Protected Methods

    # Private Methods

    def __add_user(self):
        """
        Add the user referenced by the content of the request body to the queried organization.
        :return: None
        """
        user_email = self.get_body_argument("user_email")
        self.validate_email(user_email)
        try:
            user = WsUser.objects.get(username=user_email)
            user_is_new = False
        except ObjectDoesNotExist:
            user = WsUser.objects.create(
                username=user_email,
                email=user_email,
                first_name="",
                last_name="",
            )
            user.save()
            user_is_new = True
        self.organization.set_user_permissions(user=user, permission_level="read")
        send_emails_for_org_user_invite.delay(
            org_uuid=unicode(self.organization.uuid),
            org_name=self.organization.name,
            user_uuid=unicode(user.uuid),
            user_name=user.first_name,
            user_email=user.email,
            user_is_new=user_is_new,
        )

    def __get_user_matrix(self):
        """
        Get a list of dictionaries mapping users associated with this organization to the
        permissions that those users have.
        :return: A list of dictionaries mapping users associated with this organization to the
        permissions that those users have.
        """
        user_matrix = {}
        for user in self.organization.read_group.users.all():
            if user not in user_matrix:
                user_matrix[user] = {}
            user_matrix[user]["read"] = True
        for user in self.organization.write_group.users.all():
            if user not in user_matrix:
                user_matrix[user] = {}
            user_matrix[user]["write"] = True
        for user in self.organization.scan_group.users.all():
            if user not in user_matrix:
                user_matrix[user] = {}
            user_matrix[user]["scan"] = True
        for user in self.organization.admin_group.users.all():
            if user not in user_matrix:
                user_matrix[user] = {}
            user_matrix[user]["admin"] = True
        to_return = []
        for k, v in user_matrix.iteritems():
            to_return.append({
                "user_uuid": k.uuid,
                "user_name": k.username,
                "can_read": v.get("read", False),
                "can_write": v.get("write", False),
                "can_scan": v.get("scan", False),
                "can_admin": v.get("admin", False),
            })
        return sorted(to_return, key=lambda x: x["user_name"])

    def __invite_user(self, user_email):
        """
        Create an un-activated account for the given email address, send an invitation email to
        the new user, and return the newly-created user record.
        :param user_email: The email address to create a new account for.
        :return: The newly-created user.
        """
        new_user = WsUser.objects.create(
            username=user_email,
            # Right now your username is your email, if this changes we need to change this
            email=user_email,
            first_name='',
            last_name=''
        )
        new_user.save()

        #Send the invitation email
        email_helper = SmtpEmailHelper.instance()
        email_helper.send_invite_email(user_email, str(new_user.email_registration_code), str(new_user.uuid))
        return new_user

    def __remove_user(self):
        """
        Remove the user referenced by the contents of self.request.data from all authorization
        groups associated with the referenced organization.
        :return: None
        """
        user_uuid = self.get_body_argument("user_uuid")
        self.validate_uuid(user_uuid)
        user = get_object_or_404(WsUser, pk=user_uuid)
        if self.organization.is_user_only_admin(user):
            raise OperationNotAllowed(
                "You cannot remove the only administrative user from an organization."
            )
        elif self.organization.is_only_auth_user(user):
            raise OperationNotAllowed(
                "You cannot remove the only user associated with an organization."
            )
        elif user not in self.organization.auth_users:
            raise OperationNotAllowed(
                "That user is not associated with the referenced organization."
            )
        self.organization.remove_user(user)

    def __update_user(self):
        """
        Update the user referenced by the contents of self.request.data to have the referenced
        permission level.
        :return: None
        """
        user_uuid = self.get_body_argument("user_uuid")
        permission_level = self.get_body_argument("permission_level")
        self.validate_uuid(user_uuid)
        if permission_level not in ["read", "write", "scan", "admin"]:
            raise ValidationError(
                "%s is not a valid permission level (expected read, write, scan, or admin)."
                % (permission_level,)
            )
        user = get_object_or_404(WsUser, pk=user_uuid)
        if self.organization.is_user_only_admin(user) and permission_level != "admin":
            raise OperationNotAllowed(
                "You cannot demote the only administrative user associated with an organization."
            )
        elif user == self.request.user and permission_level != "admin":
            raise OperationNotAllowed(
                "You cannot demote yourself. Please have another administrator for this organization "
                "demote you instead."
            )
        self.organization.set_user_permissions(user=user, permission_level=permission_level)

    def __validate_patch_operation(self, operation):
        """
        Validate that the contents of operation are valid for use as an operation regarding changes
        to the user model for this organization.
        :param operation: A string representing the operation in question.
        :return: None
        """
        valid_operations = ["remove_user", "add_user", "update_user"]
        if operation not in valid_operations:
            raise ValidationError(
                "%s is not a valid operation (must be one of %s)."
                % (operation, ", ".join(valid_operations))
            )

    # Properties

    # Representation and Comparison