"""Verbatim ``psSearchModel`` GraphQL document used by homedepot.com.

Captured from the live site so the document is guaranteed to validate against
Home Depot's federation gateway schema. Do not hand-edit or trim it: the
gateway rejects documents whose shape it does not recognise, and there is no
public schema to validate a rewrite against. To refresh it, re-capture the
``psSearchModel`` request the site issues and replace the constant wholesale.
"""

SEARCH_MODEL_QUERY = """\
query psSearchModel($storeId: String, $zipCode: String, $skipFavoriteCount: Boolean = false, $isBrandPricingPolicyCompliant: Boolean, $skipInstallServices: Boolean = true, $pageSize: Int, $startIndex: Int, $orderBy: ProductSort, $channel: Channel = DESKTOP, $navParam: String, $keyword: String, $itemIds: [String], $storefilter: StoreFilter = ALL, $additionalSearchParams: AdditionalParams, $loyaltyMembershipInput: LoyaltyMembershipInput) {
  searchModel(
    channel: $channel
    navParam: $navParam
    keyword: $keyword
    itemIds: $itemIds
    storefilter: $storefilter
    storeId: $storeId
    additionalSearchParams: $additionalSearchParams
    loyaltyMembershipInput: $loyaltyMembershipInput
  ) {
    id
    products(pageSize: $pageSize, startIndex: $startIndex, orderBy: $orderBy) {
      itemId
      dataSources
      info {
        hidePrice
        ecoRebate
        quantityLimit
        categoryHierarchy
        sskMin
        sskMax
        unitOfMeasureCoverage
        wasMaxPriceRange
        wasMinPriceRange
        productSubType {
          name
          link
          __typename
        }
        customerSignal {
          previouslyPurchased
          __typename
        }
        isBuryProduct
        isGenericProduct
        globalCustomConfigurator {
          customExperience
          __typename
        }
        returnable
        isLiveGoodsProduct
        isSponsored
        sponsoredMetadata {
          campaignId
          placementId
          slotId
          sponsoredId
          trackSource
          __typename
        }
        augmentedReality
        sponsoredBeacon {
          onClickBeacon
          onViewBeacon
          onClickBeacons
          onViewBeacons
          __typename
        }
        swatches {
          isSelected
          itemId
          label
          swatchImgUrl
          url
          value
          __typename
        }
        totalNumberOfOptions
        __typename
      }
      identifiers {
        itemId
        brandName
        productLabel
        productType
        canonicalUrl
        specialOrderSku
        storeSkuNumber
        modelNumber
        parentId
        __typename
      }
      fulfillment(storeId: $storeId, zipCode: $zipCode) {
        fulfillmentOptions {
          type
          fulfillable
          services {
            type
            locations {
              inventory {
                isInStock
                isOutOfStock
                isLimitedQuantity
                isUnavailable
                quantity
                maxAllowedBopisQty
                minAllowedBopisQty
                __typename
              }
              isAnchor
              curbsidePickupFlag
              isBuyInStoreCheckNearBy
              distance
              locationId
              state
              storeName
              storePhone
              type
              __typename
            }
            deliveryTimeline
            deliveryDates {
              startDate
              endDate
              __typename
            }
            deliveryCharge
            dynamicEta {
              hours
              minutes
              __typename
            }
            hasFreeShipping
            freeDeliveryThreshold
            totalCharge
            __typename
          }
          __typename
        }
        anchorStoreStatus
        anchorStoreStatusType
        backordered
        backorderedShipDate
        bossExcludedShipStates
        excludedShipStates
        seasonStatusEligible
        onlineStoreStatus
        onlineStoreStatusType
        __typename
      }
      availabilityType {
        type
        discontinued
        __typename
      }
      dataSource
      favoriteDetail @skip(if: $skipFavoriteCount) {
        count
        __typename
      }
      pricing(
        storeId: $storeId
        isBrandPricingPolicyCompliant: $isBrandPricingPolicyCompliant
      ) {
        value
        alternatePriceDisplay
        alternate {
          bulk {
            pricePerUnit
            thresholdQuantity
            value
            __typename
          }
          unit {
            caseUnitOfMeasure
            unitsOriginalPrice
            unitsPerCase
            value
            __typename
          }
          __typename
        }
        original
        mapAboveOriginalPrice
        mapDetail {
          percentageOff
          dollarOff
          effectiveMap
          mapPolicy
          mapOriginalPriceViolation
          mapSpecialPriceViolation
          __typename
        }
        message
        preferredPriceFlag
        promotion {
          type
          description {
            shortDesc
            longDesc
            __typename
          }
          dollarOff
          percentageOff
          promotionTag
          savingsCenter
          savingsCenterPromos
          specialBuySavings
          specialBuyDollarOff
          specialBuyPercentageOff
          __typename
        }
        specialBuy
        unitOfMeasure
        __typename
      }
      media {
        images {
          url
          type
          subType
          sizes
          __typename
        }
        __typename
      }
      taxonomy {
        breadCrumbs {
          label
          __typename
        }
        __typename
      }
      details {
        installation {
          serviceType
          __typename
        }
        collection {
          name
          url
          __typename
        }
        __typename
      }
      installServices(storeId: $storeId, zipCode: $zipCode) @skip(if: $skipInstallServices) {
        scheduleAMeasure
        gccCarpetDesignAndOrderEligible
        __typename
      }
      badges(storeId: $storeId) {
        name
        label
        __typename
      }
      reviews {
        ratingsReviews {
          averageRating
          totalReviews
          __typename
        }
        __typename
      }
      __typename
    }
    searchReport {
      totalProducts
      sortBy
      __typename
    }
    metadata {
      canonicalUrl
      __typename
    }
    taxonomy {
      breadCrumbs {
        dimensionName
        label
        refinementKey
        __typename
      }
      __typename
    }
    __typename
  }
}
"""
