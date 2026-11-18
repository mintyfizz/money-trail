SELECT assistance_type_code, cfda_number, cfda_title, substr(last_modified_date,1,10) AS modified_date,
 count(*) AS rows, CAST(sum(original_loan_subsidy_cost)*100 AS BIGINT) AS subsidy_cents,
 CAST(sum(face_value_of_loan)*100 AS BIGINT) AS face_cents
 FROM raw WHERE trim(awarding_agency_code)='036' AND trim(assistance_type_code) IN ('07','08','F003','F004')
 GROUP BY 1,2,3,4 ORDER BY rows DESC