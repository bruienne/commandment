'''
Copyright (c) 2015 Jesse Peterson
Licensed under the MIT license. See the included LICENSE.txt file for details.
'''

from flask import Blueprint, render_template, Response, request, redirect
from .pki.ca import get_ca, PushCertificate
from .pki.m2certs import X509Error, Certificate, RSAPrivateKey, CertificateRequest
from .database import db_session, and_, or_
from .pki.m2certs import Certificate
from .models import CERT_TYPES, profile_group_assoc, device_group_assoc, Device
from .models import Certificate as DBCertificate, PrivateKey as DBPrivateKey, MDMGroup, Profile as DBProfile, MDMConfig
from .profiles.restrictions import RestrictionsPayload
from .profiles import Profile
from .mdmcmds import InstallProfile, RemoveProfile
from mdm import send_mdm
import uuid

class FixedLocationResponse(Response):
    # override Werkzeug default behaviour of "fixing up" once-non-compliant
    # relative location headers. now permitted in rfc7231 sect. 7.1.2
    autocorrect_location_header = False

admin_app = Blueprint('admin_app', __name__)

@admin_app.route('/certificates')
def admin_certificates():
    # merely to generate new CA if not exist
    mdm_ca = get_ca()

    # get a list of configured system certificates
    cert_rows = db_session.query(DBCertificate).filter(DBCertificate.cert_type != 'mdm.device')

    # assemble a list of dictionaries to pass to our certificate list template
    installed_certs = []
    cert_output = []
    for cert_row in cert_rows:
        installed_certs.append(cert_row.cert_type)
        row_cert = Certificate.load(str(cert_row.pem_certificate))
        dict_row = {
            'id': cert_row.id,
            'name': cert_row.cert_type,
            'subject': row_cert.get_subject_as_text(),
            'title': CERT_TYPES[cert_row.cert_type]['title'] if CERT_TYPES.get(cert_row.cert_type) else '',
            'description': CERT_TYPES[cert_row.cert_type]['description'] if CERT_TYPES.get(cert_row.cert_type) else '',
            'required': bool(CERT_TYPES[cert_row.cert_type].get('required')) if CERT_TYPES.get(cert_row.cert_type) else False,
        }
        cert_output.append(dict_row)

    # assemble all the other required certificate types we know about as options
    missing = []
    for cert_name in set(CERT_TYPES.keys()).difference(set(installed_certs)):
        if cert_name != 'mdm.device':
            dict_row = {
                'name': cert_name,
                'title': CERT_TYPES[cert_name]['title'],
                'description': CERT_TYPES[cert_name]['description'],
                'required': bool(CERT_TYPES[cert_name].get('required')),
            }
            missing.append(dict_row)

    return render_template('admin/certificates/index.html', certs=cert_output, missing=missing)

@admin_app.route('/certificates/add/<certtype>', methods=['GET', 'POST'])
def admin_certificates_add(certtype):
    if certtype not in CERT_TYPES.keys():
        return 'Invalid certificate type' 
    if request.method == 'POST':
        if not request.form.get('certificate'):
            return 'No certificate supplied!'

        try:
            cert = Certificate.load(str(request.form.get('certificate')))
        except X509Error:
            return 'Invalid X509 Certificate'

        if CERT_TYPES[certtype]['pkey_required'] and not request.form.get('privatekey'):
            return 'No private key supplied (required)'

        pkey = None
        try:
            pkey = RSAPrivateKey.load(str(request.form.get('privatekey')))
        except:
            pkey = None

        if pkey:
            if not cert.belongs_to_key(pkey):
                return 'Private key does not match certificate (RSA modulus mismatch)'

        # save our newly uploaded certificates
        dbc = DBCertificate()
        dbc.cert_type = certtype
        dbc.pem_certificate = str(request.form.get('certificate'))
        db_session.add(dbc)

        # save private key if we have one
        if pkey:
            dbk = DBPrivateKey()
            dbk.pem_key = str(request.form.get('privatekey'))
            db_session.add(dbk)

            dbk.certificates.append(dbc)

        db_session.commit()

        return redirect('/admin/certificates', Response=FixedLocationResponse)
    else:
        return render_template('admin/certificates/add.html', certtype=CERT_TYPES[certtype]['title'])

@admin_app.route('/certificates/new', methods=['GET', 'POST'])
def admin_certificates_new():
    approved_certs = ['mdm.webcrt']

    if request.method == 'POST':
        print 'cert type', request.form['cert_type']
        if 'cert_type' not in request.form.keys() or request.form['cert_type'] not in approved_certs:
            abort(400, 'Invalid cert_type!')

        # all certs must have a CN?
        if 'CN' not in request.form.keys() or not request.form['CN']:
            abort(400, 'No common name!')

        approved_input = ('C', 'CN', 'OU', 'L', 'O', 'ST')

        # get dictionary of any appropriate fields submitted
        subject_names = {}
        for i in request.form.keys():
            if i in approved_input:
                subject_names.update({i: str(request.form[i])})

        print 'Generating test web certificate and CA'
        new_pk = RSAPrivateKey()

        # generate csr
        # XXX: TODO: Great unsanitized input, batman!
        new_csr = CertificateRequest(new_pk, **subject_names)

        # create (and self-sign) the web certificate request
        new_crt = Certificate.cacert_from_req(new_csr)

        # save CA private key in DB
        db_new_pk = DBPrivateKey()
        db_new_pk.pem_key = new_pk.get_pem()

        db_session.add(db_new_pk)

        # save certificate
        db_new_crt = DBCertificate()
        db_new_crt.cert_type = request.form['cert_type']
        db_new_crt.pem_certificate = new_crt.get_pem()

        db_session.add(db_new_crt)

        # add certificate to private key
        db_new_pk.certificates.append(db_new_crt)

        db_session.commit()

        # after successful addition
        return redirect('/admin/certificates', Response=FixedLocationResponse)
    else:
        cert_types = {}
        for i in approved_certs:
            cert_types[i] = CERT_TYPES[i]['title']

        return render_template('admin/certificates/new.html', cert_types=cert_types)

@admin_app.route('/certificates/delete/<int:cert_id>')
def admin_certificates_delete(cert_id):
    certq = db_session.query(DBCertificate).filter(DBCertificate.id == cert_id)
    cert = certq.one()
    db_session.delete(cert)
    db_session.commit()
    return redirect('/admin/certificates', Response=FixedLocationResponse)

@admin_app.route('/groups', methods=['GET', 'POST'])
def admin_groups():
    if request.method == 'POST':
        db_grp = MDMGroup()
        db_grp.group_name = request.form['group_name']
        db_grp.description = request.form['description']
        db_session.add(db_grp)
        db_session.commit()

        return redirect('/admin/groups', Response=FixedLocationResponse)

    groups = db_session.query(MDMGroup)

    return render_template('admin/groups.html', groups=groups)

@admin_app.route('/groups/remove/<int:group_id>')
def admin_groups_remove(group_id):
    q = db_session.query(MDMGroup).filter(MDMGroup.id == group_id).delete(synchronize_session=False)
    db_session.commit()
    return redirect('/admin/groups', Response=FixedLocationResponse)

@admin_app.route('/profiles')
def admin_profiles1():
    profiles = db_session.query(DBProfile)
    return render_template('admin/profiles/index.html', profiles=profiles)

@admin_app.route('/profiles/add', methods=['GET', 'POST'])
def admin_profiles_add1():
    if request.method == 'POST':
        config = db_session.query(MDMConfig).one()

        myrestr = RestrictionsPayload(config.prefix + '.tstRstctPld', allowiTunes=(request.form.get('allowiTunes') == 'checked'))

        # generate us a unique identifier that shouldn't change for this profile
        myidentifier = config.prefix + '.profile.' + str(uuid.uuid4())

        myprofile = Profile(myidentifier, PayloadDisplayName='Test1 Restrictions')

        myprofile.append_payload(myrestr)

        db_prof = DBProfile()

        db_prof.identifier = myidentifier
        db_prof.uuid = myprofile.get_uuid()

        db_prof.profile_data = myprofile.generate_plist()

        db_session.add(db_prof)
        db_session.commit()

        return redirect('/admin/profiles', Response=FixedLocationResponse)
    else:
        return render_template('admin/profiles/add.html')

@admin_app.route('/profiles/edit/<int:profile_id>', methods=['GET', 'POST'])
def admin_profiles_edit1(profile_id):
    # db_session
    if request.method == 'POST':
        db_prof = db_session.query(DBProfile).filter(DBProfile.id == profile_id).one()

        myprofile = Profile.from_plist(db_prof.profile_data)

        # TODO: need an API to *get* a profile out
        # TODO: assuming first payload is ours. bad, bad.
        mypld = myprofile.payloads[0]

        mypld.payload['allowiTunes'] = (request.form.get('allowiTunes') == 'checked')

        # assume changed, reset UUIDs
        myprofile.set_uuid()
        mypld.set_uuid()

        db_prof.uuid = myprofile.set_uuid()

        db_prof.profile_data = myprofile.generate_plist()

        db_session.commit()

        return redirect('/admin/profiles', Response=FixedLocationResponse)
    else:
        db_prof = db_session.query(DBProfile).filter(DBProfile.id == profile_id).one()

        # get all MDMGroups left joining against our assoc. table to see if this profile is in any of those groups
        group_q = db_session.query(MDMGroup, profile_group_assoc.c.profile_id).outerjoin(profile_group_assoc, and_(profile_group_assoc.c.mdm_group_id == MDMGroup.id, profile_group_assoc.c.profile_id == db_prof.id))

        myprofile = Profile.from_plist(db_prof.profile_data)

        # TODO: need an API to *get* a profile out
        # TODO: assuming first payload is ours. bad, bad.
        mypld = myprofile.payloads[0]

        return render_template('admin/profiles/edit.html', identifier=myprofile.get_identifier(), uuid=myprofile.get_uuid(), allowiTunes=mypld.payload['allowiTunes'], id=db_prof.id, groups=group_q)

@admin_app.route('/profiles/groupmod/<int:profile_id>', methods=['POST'])
def admin_profiles_groupmod1(profile_id):
    # get device info
    profile = db_session.query(DBProfile).filter(DBProfile.id == profile_id).one()

    # get all groups
    groups = db_session.query(MDMGroup)

    # get integer list of unique group IDs to be assigned
    new_group_memberships = set([int(g_id) for g_id in request.form.getlist('group_membership')])

    # select the groups that match the new membership ids and assign to profile
    profile.mdm_groups = [g for g in groups if g.id in new_group_memberships]

    # commit our changes
    db_session.commit()

    # TODO: trigger group membership profile commands for the *entire group*

    return redirect('/admin/profiles/edit/%d' % int(profile.id), Response=FixedLocationResponse)


@admin_app.route('/profiles/remove/<int:profile_id>')
def admin_profiles_remove1(profile_id):
    q = db_session.query(DBProfile).filter(DBProfile.id == profile_id).delete(synchronize_session=False)
    db_session.commit()
    return redirect('/admin/profiles', Response=FixedLocationResponse)

@admin_app.route('/devices')
def devices():
    devices = db_session.query(Device)

    for i in devices:
        if i.info_json is None:
            i.info_json = {}

    return render_template('admin/devices.html', devices=devices)

@admin_app.route('/device/<int:device_id>')
def admin_device(device_id):
    device = db_session.query(Device).filter(Device.id == device_id).one()

    # get all MDMGroups left joining against our assoc. table to see if this device is in any of those groups
    group_q = db_session.query(MDMGroup, device_group_assoc.c.device_id).outerjoin(device_group_assoc, and_(device_group_assoc.c.mdm_group_id == MDMGroup.id, device_group_assoc.c.device_id == device.id))

    return render_template('admin/device.html', device=device, groups=group_q)

def install_group_profiles_to_device(group, device):
    q = db_session.query(DBProfile.id).join(profile_group_assoc).filter(profile_group_assoc.c.mdm_group_id == group.id)

    # note singular tuple for subject here
    for profile_id, in q:
        new_qc = InstallProfile.new_queued_command(device, {'id': profile_id})
        db_session.add(new_qc)

def remove_group_profiles_from_device(group, device):
    q = db_session.query(DBProfile.identifier).join(profile_group_assoc).filter(profile_group_assoc.c.mdm_group_id == group.id)

    # note singular tuple for subject here
    for profile_identifier, in q:
        print 'Queueing removal of profile identifier:', profile_identifier
        new_qc = RemoveProfile.new_queued_command(device, {'Identifier': profile_identifier})
        db_session.add(new_qc)

@admin_app.route('/device/<int:device_id>/groupmod', methods=['POST'])
def admin_device_groupmod(device_id):
    # get device info
    device = db_session.query(Device).filter(Device.id == device_id).one()

    # get list of unique group IDs to be assigned
    new_group_memberships = set([int(g_id) for g_id in request.form.getlist('group_membership')])

    # get all MDMGroups left joining against our assoc. table to see if this device is in any of those groups
    group_q = db_session.query(MDMGroup, device_group_assoc.c.device_id).outerjoin(device_group_assoc, and_(device_group_assoc.c.mdm_group_id == MDMGroup.id, device_group_assoc.c.device_id == device.id))

    group_additions = []
    group_removals = []
    for group, dev_id in group_q:
        if dev_id:
            # this device is in this group currently
            if group.id not in new_group_memberships:
                # this device is being removed from this group!
                print 'Device %d is being REMOVED from Group %d (%s)!' % (device.id, group.id, group.group_name)
                group_removals.append(group)
            # else:
            #   print 'Device %d is REMAINING in Group %d (%s)!' % (device.id, group.id, group.group_name)
        else:
            # this device is NOT in this group currently
            if group.id in new_group_memberships:
                print 'Device %d is being ADDED to Group %d (%s)!' % (device.id, group.id, group.group_name)
                group_additions.append(group)
            # else:
            #   print 'Device %d is REMAINING out of Group %d (%s)!' % (device.id, group.id, group.group_name)

    # get all groups
    groups = db_session.query(MDMGroup)

    # select the groups that match the new membership ids and assign to device
    device.mdm_groups = [g for g in groups if g.id in new_group_memberships]

    # commit our changes
    db_session.commit()

    for i in group_additions:
        install_group_profiles_to_device(i, device)

    for i in group_removals:
        remove_group_profiles_from_device(i, device)

    if group_removals or group_additions:
        db_session.commit()
        send_mdm(device.id)

    return redirect('/admin/device/%d' % int(device.id), Response=FixedLocationResponse)

@admin_app.route('/config/add', methods=['GET', 'POST'])
def admin_config_add():
    mdm_ca = get_ca()

    if request.method == 'POST':
        push_cert = db_session.query(DBCertificate).filter(DBCertificate.id == int(request.form['push_cert'])).one()

        cert = PushCertificate.load(str(push_cert.pem_certificate))

        topic = cert.get_topic()

        new_config = MDMConfig()

        new_config.topic = topic

        base_url = 'https://' + request.form['hostname']

        if request.form['port']:
            portno = int(request.form['port'])

            if portno < 1 or portno > (2 ** 16):
                abort(400, 'Invalid port number')

            base_url += ':%d' % portno

        new_config.mdm_url = base_url + '/mdm'
        new_config.checkin_url = base_url + '/checkin'

        new_config.mdm_name = request.form['name']
        new_config.description = request.form['description'] if request.form['description'] else None
        new_config.prefix = request.form['prefix'].rstrip('.')

        # TODO: validate this input (but DB constraints should catch it, too)
        new_config.ca_cert_id = int(request.form['ca_cert'])

        new_config.push_cert = push_cert

        db_session.add(new_config)
        db_session.commit()

        return redirect('/admin/config/edit', Response=FixedLocationResponse)
    else:
        # get relevant certificates
        q = db_session.query(DBCertificate).join(DBPrivateKey.certificates).filter(or_(DBCertificate.cert_type == 'mdm.cacert', DBCertificate.cert_type == 'mdm.pushcert'))

        ca_certs = []
        push_certs = []
        for i in q:
            if i.cert_type == 'mdm.pushcert':
                cert = PushCertificate.load(str(i.pem_certificate))
                topic = cert.get_topic()
                i.subject_text = topic
                push_certs.append(i)
            elif i.cert_type == 'mdm.cacert':
                cert = Certificate.load(str(i.pem_certificate))
                i.subject_text = cert.get_subject_as_text()
                ca_certs.append(i)

        if not push_certs or not ca_certs:
            return redirect('/admin/certificates', Response=FixedLocationResponse)

        return render_template('admin/config/add.html', ca_certs=ca_certs, push_certs=push_certs)

@admin_app.route('/config/edit', methods=['GET', 'POST'])
def admin_config():
    config = db_session.query(MDMConfig).first()
    if not config:
        return redirect('/admin/config/add', Response=FixedLocationResponse)
    if request.method == 'POST':
        config.ca_cert_id = int(request.form['ca_cert'])
        config.mdm_name = request.form['name']
        config.description = request.form['description'] if request.form['description'] else None
        db_session.commit()
        return redirect('/admin/config/edit', Response=FixedLocationResponse)
    else:
        ca_certs = db_session.query(DBCertificate).join(DBPrivateKey.certificates).filter(DBCertificate.cert_type == 'mdm.cacert')
        for i in ca_certs:
            cert = Certificate.load(str(i.pem_certificate))
            i.subject_text = cert.get_subject_as_text()
        print type(config.description)
        return render_template('admin/config/edit.html', config=config, ca_certs=ca_certs)
