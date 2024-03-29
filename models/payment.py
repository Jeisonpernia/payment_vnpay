# coding: utf-8

import logging
import requests
import pprint

from odoo import api, fields, models, _
from odoo.addons.payment.models.payment_acquirer import ValidationError
from odoo.exceptions import UserError
from odoo.tools.safe_eval import safe_eval
from odoo.tools.float_utils import float_round

_logger = logging.getLogger(__name__)

# Force the API version to avoid breaking in case of update on Vnpay side
# cf https://vnpay.com/docs/api#versioning
# changelog https://vnpay.com/docs/upgrades#api-changelog
STRIPE_HEADERS = {'Vnpay-Version': '2016-03-07'}

# The following currencies are integer only, see https://vnpay.com/docs/currencies#zero-decimal
INT_CURRENCIES = [
    u'BIF', u'XAF', u'XPF', u'CLP', u'KMF', u'DJF', u'GNF', u'JPY', u'MGA', u'PYG', u'RWF', u'KRW',
    u'VUV', u'VND', u'XOF'
]


class PaymentAcquirerVnpay(models.Model):
    _inherit = 'payment.acquirer'

    provider = fields.Selection(selection_add=[('vnpay', 'Vnpay')])
    vnpay_secret_key = fields.Char(required_if_provider='vnpay', groups='base.group_user')
    vnpay_publishable_key = fields.Char(required_if_provider='vnpay', groups='base.group_user')
    vnpay_image_url = fields.Char(
        "Checkout Image URL", groups='base.group_user',
        help="A relative or absolute URL pointing to a square image of your "
             "brand or product. As defined in your Vnpay profile. See: "
             "https://vnpay.com/docs/checkout")

    @api.multi
    def vnpay_form_generate_values(self, tx_values):
        self.ensure_one()
        vnpay_tx_values = dict(tx_values)
        temp_vnpay_tx_values = {
            'company': self.company_id.name,
            'amount': tx_values['amount'],  # Mandatory
            'currency': tx_values['currency'].name,  # Mandatory anyway
            'currency_id': tx_values['currency'].id,  # same here
            'address_line1': tx_values.get('partner_address'),  # Any info of the partner is not mandatory
            'address_city': tx_values.get('partner_city'),
            'address_country': tx_values.get('partner_country') and tx_values.get('partner_country').name or '',
            'email': tx_values.get('partner_email'),
            'address_zip': tx_values.get('partner_zip'),
            'name': tx_values.get('partner_name'),
            'phone': tx_values.get('partner_phone'),
        }

        vnpay_tx_values.update(temp_vnpay_tx_values)
        return vnpay_tx_values

    @api.model
    def _get_vnpay_api_url(self):
        return 'api.vnpay.com/v1'

    @api.model
    def vnpay_s2s_form_process(self, data):
        payment_token = self.env['payment.token'].sudo().create({
            'cc_number': data['cc_number'],
            'cc_holder_name': data['cc_holder_name'],
            'cc_expiry': data['cc_expiry'],
            'cc_brand': data['cc_brand'],
            'cvc': data['cvc'],
            'acquirer_id': int(data['acquirer_id']),
            'partner_id': int(data['partner_id'])
        })
        return payment_token

    @api.multi
    def vnpay_s2s_form_validate(self, data):
        self.ensure_one()

        # mandatory fields
        for field_name in ["cc_number", "cvc", "cc_holder_name", "cc_expiry", "cc_brand"]:
            if not data.get(field_name):
                return False
        return True

    def _get_feature_support(self):
        """Get advanced feature support by provider.

        Each provider should add its technical in the corresponding
        key for the following features:
            * fees: support payment fees computations
            * authorize: support authorizing payment (separates
                         authorization and capture)
            * tokenize: support saving payment data in a payment.tokenize
                        object
        """
        res = super(PaymentAcquirerVnpay, self)._get_feature_support()
        res['tokenize'].append('vnpay')
        return res


class PaymentTransactionVnpay(models.Model):
    _inherit = 'payment.transaction'

    def _create_vnpay_charge(self, acquirer_ref=None, tokenid=None, email=None):
        api_url_charge = 'https://%s/charges' % (self.acquirer_id._get_vnpay_api_url())
        charge_params = {
            'amount': int(self.amount if self.currency_id.name in INT_CURRENCIES else float_round(self.amount * 100, 2)),
            'currency': self.currency_id.name,
            'metadata[reference]': self.reference,
            'description': self.reference,
        }
        if acquirer_ref:
            charge_params['customer'] = acquirer_ref
        if tokenid:
            charge_params['card'] = str(tokenid)
        if email:
            charge_params['receipt_email'] = email.strip()

        _logger.info('_create_vnpay_charge: Sending values to URL %s, values:\n%s', api_url_charge, pprint.pformat(charge_params))
        r = requests.post(api_url_charge,
                          auth=(self.acquirer_id.vnpay_secret_key, ''),
                          params=charge_params,
                          headers=STRIPE_HEADERS)
        res = r.json()
        _logger.info('_create_vnpay_charge: Values received:\n%s', pprint.pformat(res))
        return res

    @api.multi
    def vnpay_s2s_do_transaction(self, **kwargs):
        self.ensure_one()
        result = self._create_vnpay_charge(acquirer_ref=self.payment_token_id.acquirer_ref, email=self.partner_email)
        return self._vnpay_s2s_validate_tree(result)


    def _create_vnpay_refund(self):
        api_url_refund = 'https://%s/refunds' % (self.acquirer_id._get_vnpay_api_url())

        refund_params = {
            'charge': self.acquirer_reference,
            'amount': int(float_round(self.amount * 100, 2)), # by default, vnpay refund the full amount (we don't really need to specify the value)
            'metadata[reference]': self.reference,
        }

        _logger.info('_create_vnpay_refund: Sending values to URL %s, values:\n%s', api_url_refund, pprint.pformat(refund_params))
        r = requests.post(api_url_refund,
                            auth=(self.acquirer_id.vnpay_secret_key, ''),
                            params=refund_params,
                            headers=STRIPE_HEADERS)
        res = r.json()
        _logger.info('_create_vnpay_refund: Values received:\n%s', pprint.pformat(res))
        return res

    @api.multi
    def vnpay_s2s_do_refund(self, **kwargs):
        self.ensure_one()
        result = self._create_vnpay_refund()
        return self._vnpay_s2s_validate_tree(result)

    @api.model
    def _vnpay_form_get_tx_from_data(self, data):
        """ Given a data dict coming from vnpay, verify it and find the related
        transaction record. """
        reference = data.get('metadata', {}).get('reference')
        if not reference:
            vnpay_error = data.get('error', {}).get('message', '')
            _logger.error('Vnpay: invalid reply received from vnpay API, looks like '
                          'the transaction failed. (error: %s)', vnpay_error  or 'n/a')
            error_msg = _("We're sorry to report that the transaction has failed.")
            if vnpay_error:
                error_msg += " " + (_("Vnpay gave us the following info about the problem: '%s'") %
                                    vnpay_error)
            error_msg += " " + _("Perhaps the problem can be solved by double-checking your "
                                 "credit card details, or contacting your bank?")
            raise ValidationError(error_msg)

        tx = self.search([('reference', '=', reference)])
        if not tx:
            error_msg = (_('Vnpay: no order found for reference %s') % reference)
            _logger.error(error_msg)
            raise ValidationError(error_msg)
        elif len(tx) > 1:
            error_msg = (_('Vnpay: %s orders found for reference %s') % (len(tx), reference))
            _logger.error(error_msg)
            raise ValidationError(error_msg)
        return tx[0]

    @api.multi
    def _vnpay_s2s_validate_tree(self, tree):
        self.ensure_one()
        if self.state != 'draft':
            _logger.info('Vnpay: trying to validate an already validated tx (ref %s)', self.reference)
            return True

        status = tree.get('status')
        if status == 'succeeded':
            self.write({
                'date': fields.datetime.now(),
                'acquirer_reference': tree.get('id'),
            })
            self._set_transaction_done()
            self.execute_callback()
            if self.payment_token_id:
                self.payment_token_id.verified = True
            return True
        else:
            error = tree['error']['message']
            _logger.warn(error)
            self.sudo().write({
                'state_message': error,
                'acquirer_reference': tree.get('id'),
                'date': fields.datetime.now(),
            })
            self._set_transaction_cancel()
            return False

    @api.multi
    def _vnpay_form_get_invalid_parameters(self, data):
        invalid_parameters = []
        reference = data['metadata']['reference']
        if reference != self.reference:
            invalid_parameters.append(('Reference', reference, self.reference))
        return invalid_parameters

    @api.multi
    def _vnpay_form_validate(self,  data):
        return self._vnpay_s2s_validate_tree(data)


class PaymentTokenVnpay(models.Model):
    _inherit = 'payment.token'

    @api.model
    def vnpay_create(self, values):
        token = values.get('vnpay_token')
        description = None
        payment_acquirer = self.env['payment.acquirer'].browse(values.get('acquirer_id'))
        # when asking to create a token on Vnpay servers
        if values.get('cc_number'):
            url_token = 'https://%s/tokens' % payment_acquirer._get_vnpay_api_url()
            payment_params = {
                'card[number]': values['cc_number'].replace(' ', ''),
                'card[exp_month]': str(values['cc_expiry'][:2]),
                'card[exp_year]': str(values['cc_expiry'][-2:]),
                'card[cvc]': values['cvc'],
                'card[name]': values['cc_holder_name'],
            }
            r = requests.post(url_token,
                              auth=(payment_acquirer.vnpay_secret_key, ''),
                              params=payment_params,
                              headers=STRIPE_HEADERS)
            token = r.json()
            description = values['cc_holder_name']
        else:
            partner_id = self.env['res.partner'].browse(values['partner_id'])
            description = 'Partner: %s (id: %s)' % (partner_id.name, partner_id.id)

        if not token:
            raise Exception('vnpay_create: No token provided!')

        res = self._vnpay_create_customer(token, description, payment_acquirer.id)

        # pop credit card info to info sent to create
        for field_name in ["cc_number", "cvc", "cc_holder_name", "cc_expiry", "cc_brand", "vnpay_token"]:
            values.pop(field_name, None)
        return res


    def _vnpay_create_customer(self, token, description=None, acquirer_id=None):
        if token.get('error'):
            _logger.error('payment.token.vnpay_create_customer: Token error:\n%s', pprint.pformat(token['error']))
            raise Exception(token['error']['message'])

        if token['object'] != 'token':
            _logger.error('payment.token.vnpay_create_customer: Cannot create a customer for object type "%s"', token.get('object'))
            raise Exception('We are unable to process your credit card information.')

        if token['type'] != 'card':
            _logger.error('payment.token.vnpay_create_customer: Cannot create a customer for token type "%s"', token.get('type'))
            raise Exception('We are unable to process your credit card information.')

        payment_acquirer = self.env['payment.acquirer'].browse(acquirer_id or self.acquirer_id.id)
        url_customer = 'https://%s/customers' % payment_acquirer._get_vnpay_api_url()

        customer_params = {
            'source': token['id'],
            'description': description or token["card"]["name"]
        }

        r = requests.post(url_customer,
                        auth=(payment_acquirer.vnpay_secret_key, ''),
                        params=customer_params,
                        headers=STRIPE_HEADERS)
        customer = r.json()

        if customer.get('error'):
            _logger.error('payment.token.vnpay_create_customer: Customer error:\n%s', pprint.pformat(customer['error']))
            raise Exception(customer['error']['message'])

        values = {
            'acquirer_ref': customer['id'],
            'name': 'XXXXXXXXXXXX%s - %s' % (token['card']['last4'], customer_params["description"])
        }

        return values
