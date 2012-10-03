import os
import re
import itertools
from logging import getLogger

from django.conf import settings
from django.db import transaction
from django.http import *
from django.utils.translation import ugettext_lazy as _
from django.utils.datastructures import MultiValueDict
from django.contrib.auth import login

from ...pypi_config.models import Classifier
from ...pypi_config.models import DistributionType
from ...pypi_config.models import PythonVersion
from ...pypi_config.models import PlatformName
from ..metadata import METADATA_FIELDS
from ..decorators import basic_auth
from ..forms import PackageForm, ReleaseForm
from ..models import Package
from ..models import Release
from ..models import Distribution

log = getLogger(__name__)

class BadRequest(Exception):
    pass

class Forbidden(Exception):
    pass

@basic_auth
@transaction.commit_manually
def register_or_upload(request):
    try:
        _verify_post_request(request)
        package = _get_package(request)
        _verify_credentials(request, package)
        release = _get_release(request, package)
        _apply_metadata(request, release)
        response = _handle_uploads(request, release)
    except BadRequest, error:
        transaction.rollback()
        return HttpResponseBadRequest(str(error))
    except Forbidden, error:
        transaction.rollback()
        return HttpResponseForbidden(str(error))
    except Exception, error:
        transaction.rollback()
        raise

    transaction.commit()
    return HttpResponse(response)

def _verify_post_request(request):
    if request.method != 'POST':
        raise BadRequest('Only post requests are supported')

def _get_package(request):
    name = request.POST.get('name',None).strip()
    
    if not name:
        raise BadRequest('No package name specified')

    package, created = Package.objects.get_or_create(name=name)
    if created:
        package.owners.add(request.user)
        package.maintainers.add(request.user)
        package.save()

    return package

def _verify_credentials(request, package):
    if request.user not in itertools.chain(package.owners.all(), package.maintainers.all()):
        raise Forbidden('You are not an owner/maintainer of %s' % (package.name, ))

def _get_release(request, package):
    version = request.POST.get('version', '').strip()
    if not version:
        raise BadRequest('Release version must be specified')

    release, created = Release.objects.get_or_create(package=package, version=version)
    if created:
        release.save()

    return release

def _apply_metadata(request, release):
    metadata_version = request.POST.get('metadata_version', '').strip()
    if not metadata_version in METADATA_FIELDS:
        raise BadRequest('Metadata version must be present and one of: %s' % (', '.join(METADATA_FIELDS.keys()), ))

    if (('classifiers' in request.POST or 'download_url' in request.POST) and 
        metadata_version == '1.0'):
        metadata_version = '1.1'
    
    release.metadata_version = metadata_version
    
    fields = METADATA_FIELDS[metadata_version]
    
    if 'classifiers' in request.POST:
        request.POST.setlist('classifier',request.POST.getlist('classifiers'))
    
    release.package_info = MultiValueDict(dict(filter(lambda t: t[0] in fields,
                                                      request.POST.iterlists())))
    
    for key, value in release.package_info.iterlists():
        release.package_info.setlist(key,
                                     filter(lambda v: v != 'UNKNOWN', value))
    
    release.save()

def _detect_duplicate_upload(request, release, upload):
    if any(os.path.basename(dist.content.name) == uploaded.name
           for dist in release.distributions.all()):
        raise BadRequest('That file has already been uploaded...')

def _get_distribution_type(request):
    filetype, created = DistributionType.objects.get_or_create(key=request.POST.get('filetype','sdist'))
    if created:
        filetype.name = filetype.key
        filetype.save()
    return filetype

def _get_python_version(request):
    textual_pyversion = request.POST.get('pyversion','')
    if textual_pyversion == '':
        pyversion = None
    else:
        try:
            major, minor = (int(x) for x in textual_pyversion.split('.'))
        except ValueError:
            raise BadRequest('Invalid Python version number %r' % (textual_pyversion, ))
        pyversion, created = PythonVersion.objects.get_or_create(major=major, minor=minor)
        if created:
            pyversion.save()
    return pyversion

def _deduce_platform_from_filename(uploaded):
    filename_mo = re.match(r'^(?P<package_name>[\w.]+)-(?P<version>[\w.]+)-py(?P<python_version>\d+\.\d+)-(?P<platform_key>[\w.-]+)$',
                           os.path.splitext(uploaded.name)[0])
    if filename_mo is None:
        return None

    platform_key = filename_mo.groupdict()['platform_key']
    platform, created = PlatformName.objects.get_or_create(key=platform_key)
    if created:
        platform.name = platform.key
        platform.save()

    return platform

def _calculate_md5(request, uploaded):
    return request.POST.get('md5_digest', '')

def _handle_uploads(request, release):
    if not 'content' in request.FILES:
        return 'release registered'
    
    uploaded = request.FILES.get('content')
    _detect_duplicate_upload(request, release, upload)

    new_file = Distribution.objects.create(
        release    = release,
        content    = uploaded,
        filetype   = _get_distribution_type(request),
        pyversion  = _get_python_version(request),
        platform   = _deduce_platform_from_filename(uploaded),
        uploader   = request.user,
        comment    = request.POST.get('comment',''),
        signature  = request.POST.get('gpg_signature',''),
        md5_digest = _calculate_md5(request, uploaded),
    )

    return 'upload accepted'

def list_classifiers(request, mimetype='text/plain'):
    response = HttpResponse(mimetype=mimetype)
    response.write(u'\n'.join(map(lambda c: c.name,Classifier.objects.all())))
    return response

ACTION_VIEWS = dict(
    file_upload      = register_or_upload, #``sdist`` command
    submit           = register_or_upload, #``register`` command
    list_classifiers = list_classifiers, #``list_classifiers`` command
)
