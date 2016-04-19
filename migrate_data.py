# -*- coding: utf-8 -*-
import os
import sys
import json
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "bx.settings")
import django
django.setup()
from dateutil import parser
from decimal import Decimal, InvalidOperation
from django.core.validators import validate_email
from django import forms
from django.conf import settings
from bx.metadata.models import Metadata
from bx.myauth.models import User
from mod.extra import load_schema, validate_json
from bx.org.models import (
    Org, Group, Employee, OrgGroup, OrgEmployee, EmployeeUser,
    GroupEmployee, Superiority
)

DEBUG = False


def delete_empty_dict_values(dict_obj):
    d = dict()
    for k, v in dict_obj.iteritems():
        if isinstance(v, dict):
            v = delete_empty_dict_values(v)
        if isinstance(v, list):
            v = delete_empty_list_values(v)
        if v:
            d[k] = v
    return d


def delete_empty_list_values(list_obj):
    l = list()
    for v in list_obj:
        if isinstance(v, list):
            v = delete_empty_list_values(v)
        if isinstance(v, dict):
            v = delete_empty_dict_values(v)
        if v:
            l.append(v)
    return l


def is_valid_email(email):
    try:
        validate_email(email)
    except forms.ValidationError:
        return False

    return True


class Singleton(type):
    _instances = {}

    def __call__(cls, *args, **kwargs):
        if cls not in cls._instances:
            cls._instances[cls] = super(Singleton, cls).__call__(*args,
                                                                 **kwargs)
        return cls._instances[cls]


class BaseManager(object):
    __metaclass__ = Singleton

    def __init__(self, old_data=None, json_schema=None):
        """
        :param old_data: old data for model
        :param json_schema: json schema validator
        :return:
        """
        self.data = old_data
        self.schema = json_schema

    def get_extra_by_schema(self, extra_data, schema_object):
        extra_fields = schema_object.keys()
        return {k: v for k, v in extra_data.iteritems() if k in extra_fields}

    def get_extra_legacy(self, extra_data, legacy_model_name=None):
        """ Get from dict only elements which is in json schema
        :param extra_data: dict
        :param legacy_model_name: if model include some old models
        :return: dict
        """
        schema_legacy = self.schema['properties']['legacy']['properties']
        if legacy_model_name:
            schema_legacy = schema_legacy[legacy_model_name]['properties']
        return self.get_extra_by_schema(extra_data, schema_legacy)

    def clean_and_validate_extra(self, extra):
        extra = delete_empty_dict_values(extra)
        validate_json(extra, self.schema)
        return extra

    def get_or_create(self, **kwargs):
        raise NotImplementedError()


class UserManager(BaseManager):
    def get_by_old_user_id(self, user_id):
        return self.get_or_create(**self.data[user_id])

    def get_or_create(self, email, **kwargs):
        """ Get or create User from json
        :param email: email
        :param kwargs: params for extra field
        :return: User object
        """
        def get_email():
            # if email doesn't exist, generate it
            return email or '{0}@beneple.com'.format(kwargs['username'])

        try:
            obj = User.objects.get(email=get_email())
        except User.DoesNotExist:
            obj = User(
                email=get_email(), password=kwargs['password'],
                clearance=self.convert_group_permission(kwargs['groups']),
                extra={'legacy': {
                    'date_joined': kwargs['date_joined'],
                    'first_name': kwargs['first_name'],
                    'last_name': kwargs['last_name'],
                    'username': kwargs['username'],
                }}
            )
            obj.save(metadata=Metadata.empty())

        return obj

    @staticmethod
    def convert_group_permission(group_ids):
        old_groups = []
        for group in group_ids:
            old_groups.append({
                1: 'Admin',
                4: 'CLevel',
                6: 'Employee',
                3: 'HR',
                5: 'LineManager',
                2: 'SuperHR',
            }.get(group))
        if 'HR' in old_groups or 'SuperHR' in old_groups:
            return 'edit'
        elif 'Admin' in old_groups:
            return 'audit'
        else:
            return ''


class OrgManager(BaseManager):
    def get_or_create(self, title, url, **kwargs):
        """ Get or create Org from json.
        :param title: name filed
        :param url: domain field
        :param kwargs: other params
        :return: Org object
        """
        try:
            obj = Org.objects.get(name=title, domain=url)
        except Org.DoesNotExist:
            extra_legacy = self.get_extra_legacy(kwargs)
            obj = Org(name=title, domain=url, extra={'legacy': extra_legacy})

            working_start_hour = kwargs.get('working_start_hour')
            if working_start_hour:
                obj.extra["working_hours_start"] = parser.parse(
                    working_start_hour).time().isoformat()
            working_hours_end = kwargs.get('working_hours_end')
            if working_hours_end:
                obj.extra["working_hours_end"] = parser.parse(
                    working_hours_end).time().isoformat()

            validate_json(obj.extra, self.schema)
            obj.save(metadata=Metadata.empty())

        return obj


class EmployeeManager(BaseManager):
    def get_by_old_employee_id(self, employee_id):
        old_user_id = self.data[employee_id]['user']
        user = UserManager().get_by_old_user_id(old_user_id)
        return Employee.objects.get(employeeuser__user=user)

    def get_user_by_old_employee_id(self, employee_id):
        old_user_id = self.data[employee_id]['user']
        return UserManager().get_by_old_user_id(old_user_id)

    def get_or_create(self, user_obj, org_obj=None, **kwargs):
        """ Get or create Employee object.
        :param user_obj: User model instance
        :param org_obj: Org model instance
        :param kwargs: other params
        :return: Employee object
        """
        try:
            obj = Employee.objects.get(employeeuser__user=user_obj)
        except Employee.DoesNotExist:
            m = Metadata.empty()
            pers_email = kwargs['personal_email']
            gender = kwargs['gender']
            obj = Employee(
                displayname=u'{0} {1}'.format(
                    user_obj.extra['legacy']['first_name'],
                    user_obj.extra['legacy']['last_name']
                ).strip(),
                active=kwargs['status'],
                extra=delete_empty_dict_values({
                    'birth_date': kwargs['birth_date'],
                    'emergency_contact': {
                        'name': kwargs['ec_name'],
                        'phone_number': kwargs['ec_phone'],
                    },
                    'gender': gender.lower() if gender else None,
                    'contact': {'home': {
                        'address': kwargs['home_address'],
                        'email': pers_email if is_valid_email(pers_email) else '',
                        'phone_number': kwargs['phone'],
                    }},
                    'documents': {
                        'labor_card': {
                            'number': kwargs['labour_card'],
                            'expiry_date': kwargs['labour_expiry_date'],
                        },
                        'passport': {
                            'number': kwargs['passport_number'],
                            'expiry_date': kwargs['passport_expiry_date'],
                        },
                        'emirates_id': {
                            'number': kwargs['emirates_id'],
                            'expiry_date': kwargs['emirates_id_expiry_date'],
                        },
                    },
                    'languages': [
                            lang.strip() for lang in kwargs['languages'].split(',')
                        ] if kwargs['languages'] else [],

                    'nationality': kwargs['nationality'],

                    'legacy': {
                        'employee': self.get_extra_legacy(kwargs, 'employee')
                    }
                }),
            )
            obj.extra['marital_status'] = {
                'Married': 'married',
                'Single': 'unmarried',
            }.get(kwargs['marital_status']) or "other"

            obj.extra['religion'] = {
                'Christianity': 'Christian',
                'Islam': 'Muslim',
            }.get(kwargs['religion']) or 'Other'

            validate_json(obj.extra, self.schema)
            obj.save(metadata=m)

            EmployeeUser(employee=obj, user=user_obj).save(metadata=m)
            if org_obj:
                OrgEmployee(employee=obj, org=org_obj).save(metadata=m)

        return obj

    def complement_from_employment(self, employee_obj, **kwargs):
        """ Update Employee object, get data from employee.employment
        :param employee_obj: Employee model instance
        :param kwargs: data params
        :return:
        """
        obj = employee_obj
        try:
            salary = Decimal(kwargs['salary'].replace(',', '')) \
                if kwargs['salary'] else Decimal(0)
            if len(salary.as_tuple().digits) > 10:
                # if count of digits more 10 - don't save in db
                salary = Decimal(0)
            obj.monthlysalary = salary
        except InvalidOperation:
            pass

        obj.extra['title'] = kwargs['position'] or '---'
        if 'documents' not in obj.extra:
            obj.extra['documents'] = {}
        obj.extra['documents'].update({
            'visa': {
                'number': kwargs['visa_number'],
                'expiry_date': kwargs['visa_expiry_date'],
                'image_url': kwargs['visa_document'],
            },
            'work_contract': {
                'start_date': kwargs['contract_start_date'],
                'end_date': kwargs['contract_end_date'],
            },
        })
        extra_legacy = self.get_extra_legacy(kwargs, 'employment')
        obj.extra['legacy'].update({'employment': extra_legacy})

        obj.extra = self.clean_and_validate_extra(obj.extra)
        obj.save(metadata=Metadata.empty())

        return obj

    def complement_form_bankinfo(self, employee_obj, **kwargs):
        obj = employee_obj
        bank_schema = self.schema['properties']['bank_account']['properties']
        obj.extra['bank_account'] = self.get_extra_by_schema(kwargs,
                                                             bank_schema)
        obj.extra['legacy']['bank_account'] = self.get_extra_legacy(
            kwargs, 'bank_account'
        )
        obj.extra = self.clean_and_validate_extra(obj.extra)
        obj.save(metadata=Metadata.empty())

        return obj

    def complement_form_dependent(self, employee_obj, **kwargs):
        obj = employee_obj
        obj.extra.setdefault('dependants', [])
        dependant_names = [d['name'] for d in obj.extra['dependants']]
        if kwargs['name'] not in dependant_names:
            relation = kwargs['relation']
            relation = relation if relation and relation != 'None' else 'Other'
            obj.extra['dependants'].append({
                'birth_date': kwargs['birth_date'],
                'name': kwargs['name'],
                'nationality': kwargs['nationality'],
                'relation': relation,
                'emirates_id': {
                    'number': kwargs['emirates_id'],
                    'expiry_date': kwargs['emirates_id_expiry_date'],
                },
                'passport': {
                    'number': kwargs['passport_number'],
                    'expiry_date': kwargs['passport_expiry_date'],
                },
                'visa': {
                    'number': kwargs['visa_number'],
                    'expiry_date': kwargs['visa_expiry_date'],
                    'image_url': kwargs['visa_document']
                },
            })
        obj.extra['legacy']['dependants'] = self.get_extra_legacy(
            kwargs, 'dependants'
        )

        obj.extra = self.clean_and_validate_extra(obj.extra)
        obj.save(metadata=Metadata.empty())

        return obj


def load_old_data():
    with open(os.path.join(settings.BASE_DIR, 'old_db_data.json')) as f:
        json_data = json.load(f)

    # loading old data
    data = {}
    for i in json_data:
        data.setdefault(i['model'], {})
        data[i['model']].update({i['pk']: i['fields']})
    print 'data for import loaded'

    # load json schema
    org_schema = load_schema(__file__, 'org/org.schema.json')
    employee_schema = load_schema(__file__, 'org/employee.schema.json')
    del(employee_schema['required'])

    # init converters
    user_manager = UserManager(data['auth.user'])
    org_manager = OrgManager(data['aythan.organization'], org_schema)
    employee_manager = EmployeeManager(data['employee.employee'],
                                       employee_schema)

    # import auth.user
    for user_id, user_data in data['auth.user'].iteritems():
        user_manager.get_or_create(**user_data)
        if DEBUG:
            break
    print 'import auth.user is complete'

    # import aythan.organization
    for org_id, org_data in data['aythan.organization'].iteritems():
        user_id = org_data.get('user')
        if user_id:
            # link to org user equivalent of admin rights in new systems
            user = user_manager.get_by_old_user_id(user_id)
            user.clearance = 'admin'
            user.save(metadata=Metadata.empty())

        org_manager.get_or_create(**org_data)

        if DEBUG:
            break
    print 'import aythan.organization is complete'

    # load employee.employee
    for employee_id, employee_data in data['employee.employee'].iteritems():
        user = user_manager.get_by_old_user_id(employee_data['user'])
        org = None
        org_id = employee_data.get('organization')
        if org_id:
            org = org_manager.get_or_create(
                **data['aythan.organization'][org_id]
            )

        employee_manager.get_or_create(user_obj=user, org_obj=org,
                                       **employee_data)

        if DEBUG:
            break
    print 'import employee.employee is complete'

    # load employee.employment
    for e_id, employment_data in data['employee.employment'].iteritems():
        employee = employee_manager.get_by_old_employee_id(
            employment_data['employee']
        )
        if employee_data.get('line_manager'):
            # create Superiority link for User
            line_manager_user = employee_manager.get_user_by_old_employee_id(
                employee_data['line_manager'])
            line_manager = employee_manager.get_or_create(
                user_obj=line_manager_user)
            Superiority(
                subordinate=employee, superior=line_manager
            ).save(metadata=Metadata.empty())

        employee_manager.complement_from_employment(
            employee, **employment_data
        )

        if DEBUG:
            break
    print 'import employee.employment is complete'

    # load employee.bankinfo
    for eb_id, eb_data in data['employee.bankinfo'].iteritems():
        employee = employee_manager.get_by_old_employee_id(eb_data['employee'])
        employee_manager.complement_form_bankinfo(employee, **eb_data)

        if DEBUG:
            break
    print 'import employee.bankinfo is complete'

    # load employee.dependent
    for ed_id, ed_data in data['employee.dependent'].iteritems():
        employee = employee_manager.get_by_old_employee_id(ed_data['employee'])
        employee_manager.complement_form_dependent(employee, **ed_data)

        if DEBUG:
            break
    print 'import employee.dependent is complete'


if __name__ == "__main__":
    load_old_data()
