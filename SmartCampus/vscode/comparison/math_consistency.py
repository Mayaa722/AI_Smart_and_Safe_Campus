"""Experimental deterministic guard for a line with finite nonzero axis intercepts.

Checks arithmetic consistency, not whether an LLM read the diagram correctly.
Never silently overwrites a model answer or uses a development reference as input.
"""
import math


def check_normal_form(x_intercept,y_intercept,rho,theta_degrees,rho_tolerance=.15,angle_tolerance=3):
    values=[x_intercept,y_intercept,rho,theta_degrees]
    if any(v is None or not math.isfinite(v) for v in values):
        return {'status':'unverified','reason':'A finite measured value is missing.'}
    if x_intercept==0 or y_intercept==0:
        return {'status':'unverified','reason':'This intercept form cannot represent a zero or infinite intercept.'}
    a,b=1/x_intercept,1/y_intercept
    expected_rho=1/math.hypot(a,b)
    expected_theta=math.degrees(math.atan2(b,a))%360
    normalized_theta=theta_degrees%360
    angle_error=abs((normalized_theta-expected_theta+180)%360-180)
    # (-rho, theta+180) describes the same line.
    if rho<0:
        rho=-rho;normalized_theta=(normalized_theta+180)%360
        angle_error=abs((normalized_theta-expected_theta+180)%360-180)
    consistent=abs(rho-expected_rho)<=rho_tolerance and angle_error<=angle_tolerance
    return {'status':'consistent' if consistent else 'needs_review',
        'rho_from_reported_intercepts':expected_rho,'theta_degrees_from_reported_intercepts':expected_theta,
        'rho_absolute_error':abs(rho-expected_rho),'theta_absolute_error_degrees':angle_error,
        'tolerances':{'rho_absolute':rho_tolerance,'theta_degrees':angle_tolerance},
        'limitation':'Does not verify the image reading, source formula, or physical assumptions. Engineering tolerances are not paper metrics.'}


if __name__=='__main__':
    from pathlib import Path
    import json
    root=Path(__file__).resolve().parents[1]/'run/comparison_v1'
    actual=check_normal_form(4,3,2.15,45)
    assert actual['status']=='needs_review'
    analytic=check_normal_form(4.5,3,9/math.sqrt(13),math.degrees(math.atan(1.5)))
    assert analytic['status']=='consistent'
    assert check_normal_form(None,3,2.5,56)['status']=='unverified'
    result={'development_example':'Hough page 26, actual Gemini 3.5 visual response',
       'annotation_provenance':'Assistant read the reported intercepts and midpoint of reported rho range from saved output; not automatic extraction.',
       'reported_values':{'x_intercept':4,'y_intercept':3,'rho_range':[2.1,2.2],'theta_degrees':45},
       'check':actual,'reference_image_reading':'Assistant inspected original plot: approximately x=4.5, y=3; requires independent instructor confirmation.',
       'intervention_scope':'Post-hoc experimental guard; not a preregistered metric and not integrated into the production tutor yet.'}
    (root/'math_consistency_probe.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    print(actual)
